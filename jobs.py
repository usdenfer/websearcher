"""Scheduled search jobs: JSON persistence, due computation, execution.

Jobs are user-defined recurring searches (daily at HH:MM or every N hours).
Each run crawls and matches like /api/search (auto render escalation
included), then diffs hit keys against the previous run to surface
newly appeared hits (newHits). First run establishes the baseline
(newHits=None).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

JOBS_FILE = Path(__file__).parent / "data" / "jobs.json"
STATIC_BUDGET_SECONDS = 120
RENDER_BUDGET_SECONDS = 600
SITESEARCH_BUDGET_SECONDS = 45
AUTO_LOW_HITS = 3
MAX_HIT_KEYS = 500
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_schedule(schedule: dict) -> dict:
    kind = schedule.get("kind")
    if kind == "daily":
        time_str = str(schedule.get("time", ""))
        if not TIME_RE.match(time_str):
            raise ValueError("每日任务需要 HH:MM 格式的时间")
        return {"kind": "daily", "time": time_str}
    if kind == "interval":
        hours = schedule.get("hours")
        if not isinstance(hours, int) or not 1 <= hours <= 168:
            raise ValueError("间隔小时数必须在 1~168 之间")
        return {"kind": "interval", "hours": hours}
    raise ValueError("schedule.kind 必须是 daily 或 interval")


def make_job(start_url: str, keywords: list[str], depth: int,
             render: str, schedule: dict, name: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "startUrl": start_url,
        "keywords": keywords,
        "depth": depth,
        "render": render,
        "schedule": validate_schedule(schedule),
        "enabled": True,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "lastRunAt": None,
        "prevKeys": [],
        "lastResult": None,
        "lastError": None,
        "running": False,
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_due(job: dict, now: datetime) -> bool:
    if not job.get("enabled", True):
        return False
    sch = job["schedule"]
    last = _parse_dt(job.get("lastRunAt"))
    if sch["kind"] == "interval":
        return last is None or now >= last + timedelta(hours=sch["hours"])
    hh, mm = map(int, sch["time"].split(":"))
    today_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < today_run:
        return False
    if last is None:
        return True
    return last < today_run


def next_run_at(job: dict, now: datetime) -> str:
    sch = job["schedule"]
    last = _parse_dt(job.get("lastRunAt"))
    if sch["kind"] == "interval":
        base = last or _parse_dt(job.get("createdAt")) or now
        moment = base + timedelta(hours=sch["hours"])
    else:
        hh, mm = map(int, sch["time"].split(":"))
        moment = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if moment <= now and last is not None and last >= moment:
            moment += timedelta(days=1)
    if moment <= now:
        return now.isoformat(timespec="seconds")
    return moment.isoformat(timespec="seconds")


def describe_schedule(schedule: dict) -> str:
    if schedule["kind"] == "daily":
        return f"每天 {schedule['time']}"
    return f"每 {schedule['hours']} 小时"


def hit_keys(results: list[dict]) -> set[str]:
    keys: set[str] = set()
    for page in results:
        for hit in page["hits"]:
            raw = f"{page['pageUrl']}|{hit['kind']}|{hit['snippet']}"
            keys.add(hashlib.sha1(raw.encode("utf-8")).hexdigest())
    return keys


class JobStore:
    def __init__(self, path: Path = JOBS_FILE):
        self.path = Path(path)
        self._jobs: list[dict] = []
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._jobs = json.loads(
                    self.path.read_text(encoding="utf-8")).get("jobs", [])
            except (json.JSONDecodeError, OSError):
                self._jobs = []
        else:
            self._jobs = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"jobs": self._jobs}, ensure_ascii=False,
                                  indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        return list(self._jobs)

    def get(self, job_id: str) -> dict | None:
        return next((j for j in self._jobs if j["id"] == job_id), None)

    def add(self, job: dict) -> dict:
        self._jobs.append(job)
        self.save()
        return job

    def remove(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j["id"] != job_id]
        if len(self._jobs) != before:
            self.save()
            return True
        return False

    def update(self, job: dict) -> None:
        for i, j in enumerate(self._jobs):
            if j["id"] == job["id"]:
                self._jobs[i] = job
                self.save()
                return
        raise KeyError(job["id"])


async def run_job(store: JobStore, job_id: str,
                  crawl_fn=None, expand_fn=None, sitesearch_fn=None) -> dict:
    """Execute one scheduled search. Returns a compact run summary;
    job record is updated in the store either way."""
    if crawl_fn is None:
        from crawler import crawl as crawl_fn
    if expand_fn is None:
        from ai import expand_keywords as expand_fn
    if sitesearch_fn is None:
        from sitesearch import collect_pages as sitesearch_fn

    job = store.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.get("running"):
        return {"error": "任务正在运行中"}
    job["running"] = True
    store.update(job)
    try:
        host = urlsplit(job["startUrl"]).netloc
        expanded = await expand_fn(job["keywords"], host)
        all_keywords = job["keywords"] + [
            k for k in expanded if k not in job["keywords"]]

        render_used = False
        mode = job.get("render", "auto")
        budget = (RENDER_BUDGET_SECONDS if mode == "on"
                  else STATIC_BUDGET_SECONDS)
        try:
            if mode == "on":
                async with asyncio.timeout(RENDER_BUDGET_SECONDS):
                    crawl_result = await crawl_fn(
                        job["startUrl"], depth=job["depth"], render=True)
                render_used = True
            else:
                async with asyncio.timeout(STATIC_BUDGET_SECONDS):
                    crawl_result = await crawl_fn(
                        job["startUrl"], depth=job["depth"], render=False)
                if mode == "auto" and crawl_result.pages:
                    from matcher import looks_js_driven, match_crawl_result
                    _, static_hits = match_crawl_result(
                        crawl_result.pages, all_keywords)
                    if static_hits == 0 or (
                            looks_js_driven(crawl_result.pages[0].html)
                            and static_hits <= AUTO_LOW_HITS):
                        async with asyncio.timeout(RENDER_BUDGET_SECONDS):
                            render_result = await crawl_fn(
                                job["startUrl"], depth=job["depth"],
                                render=True)
                        _, render_hits = match_crawl_result(
                            render_result.pages, all_keywords)
                        if render_hits > static_hits:
                            crawl_result = render_result
                            render_used = True
        except Exception as exc:  # noqa: BLE001 - recorded on the job
            error = str(exc) or type(exc).__name__
            job["lastError"] = error
            job["lastRunAt"] = datetime.now().isoformat(timespec="seconds")
            return {"error": error}

        from matcher import match_crawl_result
        try:
            async with asyncio.timeout(SITESEARCH_BUDGET_SECONDS):
                extra_pages, _ss_info = await sitesearch_fn(
                    job["startUrl"], all_keywords,
                    skip={p.url for p in crawl_result.pages})
            crawl_result.pages.extend(extra_pages)
        except Exception:  # noqa: BLE001 - 站内搜索失败不影响任务
            pass
        results, total_hits = match_crawl_result(
            crawl_result.pages, all_keywords)
        keys = hit_keys(results)
        prev = set(job.get("prevKeys") or [])
        new_hits = None if job.get("lastResult") is None else len(keys - prev)

        summary = {
            "ranAt": datetime.now().isoformat(timespec="seconds"),
            "pagesCrawled": len(crawl_result.pages),
            "totalHits": total_hits,
            "newHits": new_hits,
            "renderUsed": render_used,
            "top": [{"pageUrl": r["pageUrl"], "pageTitle": r["pageTitle"],
                     "hitCount": len(r["hits"])} for r in results[:5]],
        }
        job["lastResult"] = summary
        job["lastError"] = None
        job["lastRunAt"] = summary["ranAt"]
        job["prevKeys"] = sorted(keys)[:MAX_HIT_KEYS]
        return summary
    finally:
        job["running"] = False
        store.update(job)
