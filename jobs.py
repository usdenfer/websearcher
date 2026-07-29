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
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from crawler import ARCHIVE_BUDGET_SECONDS, ARCHIVE_MAX_PAGES

JOBS_FILE = Path(__file__).parent / "data" / "jobs.json"
SEARCH_BUDGET_SECONDS = 120
BASE_BFS_PAGE_BUDGET = 30
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


def _require_crawl_pages(crawl_result) -> None:
    if crawl_result.pages:
        return
    reason = (
        crawl_result.failed[0].get("reason", "未获取到起始页")
        if crawl_result.failed
        else "未获取到起始页"
    )
    raise RuntimeError(f"起始页无法访问：{reason}")


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
                  crawl_fn=None, expand_fn=None, discovery_fn=None,
                  archive_fn=None) -> dict:
    """Execute one scheduled search. Returns a compact run summary;
    job record is updated in the store either way."""
    if crawl_fn is None:
        from crawler import crawl as crawl_fn
    if expand_fn is None:
        from ai import expand_keywords as expand_fn
    if discovery_fn is None:
        from discovery import discover_pages as discovery_fn
    if archive_fn is None:
        from crawler import crawl_archive as archive_fn

    job = store.get(job_id)
    if job is None:
        raise KeyError(job_id)
    if job.get("running"):
        return {"error": "任务正在运行中"}
    job["running"] = True
    store.update(job)
    try:
        from discovery import BudgetManager

        mode = job.get("render", "auto")
        archive_mode = mode == "archive"
        search_started = time.monotonic()
        budget = BudgetManager(
            initial_pages=ARCHIVE_MAX_PAGES if archive_mode else 60,
            max_pages=ARCHIVE_MAX_PAGES if archive_mode else 120,
            timeout_seconds=(
                ARCHIVE_BUDGET_SECONDS if archive_mode
                else SEARCH_BUDGET_SECONDS),
            started_at=search_started,
        )
        deadline = budget.deadline
        host = urlsplit(job["startUrl"]).netloc
        from crawler import await_before_deadline
        expanded_value, expansion_timed_out = await await_before_deadline(
            expand_fn(job["keywords"], host), deadline
        )
        expanded = [] if expansion_timed_out else expanded_value
        all_keywords = job["keywords"] + [
            k for k in expanded if k not in job["keywords"]]

        render_used = False
        try:
            if mode == "archive":
                crawl_result = await archive_fn(
                    job["startUrl"],
                    deadline=deadline,
                    budget=budget,
                )
                _require_crawl_pages(crawl_result)
                render_used = True
            elif mode == "on":
                crawl_result = await crawl_fn(
                    job["startUrl"],
                    depth=job["depth"],
                    max_pages=BASE_BFS_PAGE_BUDGET,
                    render=True,
                    deadline=deadline,
                    budget=budget,
                )
                _require_crawl_pages(crawl_result)
                render_used = True
            else:
                crawl_result = await crawl_fn(
                    job["startUrl"],
                    depth=job["depth"],
                    max_pages=BASE_BFS_PAGE_BUDGET,
                    render=False,
                    deadline=deadline,
                    budget=budget,
                )
                _require_crawl_pages(crawl_result)
                if mode == "auto" and crawl_result.pages:
                    from matcher import (
                        looks_js_driven,
                        match_body_crawl_result,
                    )
                    _, static_hits = match_body_crawl_result(
                        crawl_result.pages, all_keywords)
                    if static_hits == 0 or (
                            looks_js_driven(crawl_result.pages[0].html)
                            and static_hits <= AUTO_LOW_HITS):
                        render_result = await crawl_fn(
                            job["startUrl"],
                            depth=job["depth"],
                            max_pages=BASE_BFS_PAGE_BUDGET,
                            render=True,
                            deadline=deadline,
                            budget=budget,
                        )
                        _, render_hits = match_body_crawl_result(
                            render_result.pages, all_keywords)
                        if render_hits > static_hits:
                            crawl_result = render_result
                            render_used = True
        except Exception as exc:  # noqa: BLE001 - recorded on the job
            error = str(exc) or type(exc).__name__
            job["lastError"] = error
            job["lastRunAt"] = datetime.now().isoformat(timespec="seconds")
            return {"error": error}

        from discovery.urltools import normalize_candidate_url
        from matcher import match_body_crawl_result
        if archive_mode:
            # 归档深扫已全量发现，跳过 discovery 避免重复翻页
            discovery_run = None
        else:
            try:
                discovery_run = await discovery_fn(
                    job["startUrl"],
                    all_keywords,
                    crawl_result,
                    job["depth"],
                    mode,
                    budget=budget,
                )
                seen = {
                    normalize_candidate_url(page.url)
                    for page in crawl_result.pages
                }
                for page in discovery_run.pages:
                    normalized = normalize_candidate_url(page.url)
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    crawl_result.pages.append(page)
                crawl_result.failed.extend(discovery_run.failed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 站内搜索失败不影响任务
                pass
        results, total_hits = match_body_crawl_result(
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
