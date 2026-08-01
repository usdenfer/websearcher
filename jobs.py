"""Scheduled search jobs: JSON persistence, due computation, execution.

Jobs are user-defined recurring searches (daily at HH:MM or every N hours).
Each run crawls and matches like /api/search (auto render escalation
included), then diffs hit keys against the previous run to surface
newly appeared hits (newHits). First run establishes the baseline
(newHits=None). After each non-baseline run, an AI summary of new
entries is generated and stored on the job.
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

from search_budget import (
    BASE_BFS_PAGE_BUDGET,
    make_search_budget,
)

JOBS_FILE = Path(__file__).parent / "data" / "jobs.json"
AUTO_LOW_HITS = 3
MAX_HIT_KEYS = 500
MAX_AI_ENTRIES = 20
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


def make_job(start_url: str, keywords: list[str],
             render: str, schedule: dict, name: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "startUrl": start_url,
        "keywords": keywords,
        "depth": 3,
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


def _hit_key(page_url: str, hit: dict) -> str:
    raw = f"{page_url}|{hit['kind']}|{hit['snippet']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _filter_new_hit_results(results: list[dict],
                            prev_keys: set[str]) -> list[dict]:
    """Return only pages that have at least one hit not in prev_keys,
    keeping only the new hits on each page."""
    if not prev_keys:
        return []
    new_results: list[dict] = []
    for page in results:
        new_hits = [h for h in page["hits"]
                    if _hit_key(page["pageUrl"], h) not in prev_keys]
        if new_hits:
            new_page = dict(page)
            new_page["hits"] = new_hits
            new_results.append(new_page)
    return new_results


def _parse_published_date(value: str | None) -> datetime | None:
    """Parse a publishedDate string into a datetime; None on failure."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip()[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _filter_results_by_time(results: list[dict],
                            since: datetime | None) -> list[dict]:
    """Return only results whose publishedDate >= since.
    Pages without a publishedDate are kept (conservative)."""
    if since is None:
        return results
    filtered: list[dict] = []
    for page in results:
        pub = _parse_published_date(page.get("publishedDate"))
        if pub is None or pub >= since:
            filtered.append(page)
    return filtered


def _build_job_summary_entries(new_results: list[dict],
                               crawled_pages,
                               ) -> list[dict]:
    """Build structured entry cards from new-hit pages and crawled HTML,
    mirroring the manual-summarize entry format."""
    from matcher import extract_main_text

    url_to_html: dict[str, str] = {}
    for cp in crawled_pages:
        url_to_html[cp.url] = cp.html

    seen: set[str] = set()
    entries: list[dict] = []
    for r in new_results[:MAX_AI_ENTRIES]:
        url = r.get("pageUrl", "")
        if not url or url in seen:
            continue
        seen.add(url)
        hits = r.get("hits", [])
        if not hits:
            continue
        snippet = _pick_best_snippet(hits)
        date = r.get("publishedDate", "")
        full_text = extract_main_text(url_to_html.get(url, ""))
        summary = _build_structured_summary(hits, full_text, snippet)
        title = _make_entry_title(r.get("pageTitle") or url, url, summary)
        if not summary:
            continue
        entries.append(dict(
            date=date, title=title, summary=summary, link=url,
        ))
    entries.sort(key=lambda e: e["date"] or "", reverse=True)
    return entries


# ── helpers ported from server.py for structured summaries ──

_PROJECT_TERMS = re.compile(
    r'项目|采购|招标|中标|成交|合同|工程|服务|设备|施工|监理|设计|公告|公示')
_NOISE_TERMS = re.compile(
    r'(?:电话|咨询|支持|联系|投诉|监督|地址|邮编|传真|邮箱|客服|热线|'
    r'工作日|办理|CA|数字证书|登录|注册|密码)')
_PHONE_RE = re.compile(r'\b\d{3,4}[-–—]\d{7,8}\b')

_SITE_NAME_RE = re.compile(
    r'(?:政府采购网|采购网|招标网|公共资源|政府门户|人民政府|'
    r'政务服务平台|政务服务网|交易平台|交易中心|'
    r'信息网|服务网|门户网站)$')

_TITLE_PATTERNS = [
    re.compile(r'(?:采购)?项目名称[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'项目编号[：:][^。；;\n]*?项目名称[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'采购(?:意向|需求|内容)[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'(?:^|[。；;，,、\n])\s*([^。；;，,、\n]{10,60}?(?:项目|采购项目|招标项目|中标|成交|合同))'),
    re.compile(r'项目概况[：:]?\s*\n?\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'(.{8,50}(?:项目|采购|招标|中标|施工|监理|设备).{0,30})'),
]


def _pick_best_snippet(hits: list[dict]) -> str:
    best = ""
    best_score = 0
    for h in hits:
        s = h.get("snippet", "").strip()
        if not s:
            continue
        score = min(len(s), 200)
        if _PROJECT_TERMS.search(s):
            score += 100
        if _NOISE_TERMS.search(s):
            score -= 200
        if score > best_score:
            best_score = score
            best = s
    return best[:200] if best and best_score > 0 else ""


def _clean_page_text(text: str) -> str:
    """Strip header/footer noise lines (same as server.py)."""
    lines = text.split('\n')
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) < 12 and re.search(
            r'^(?:首页|网站|联系|关于|设为|加入|收藏|主办|承办|技术支持|'
            r'建议|分辨率|浏览器|当前位置|今天是|欢迎|访问|进入|返回|登录|'
            r'注册|版权所有|All Rights|Copyright|ICP|备案)', stripped):
            continue
        if re.search(r'(?:技术支持|咨询电话|服务热线|监督电话|投诉电话|'
                     r'CA\s*数字证书|办理地点|工作日|上午|下午|邮箱|传真|邮编)', stripped):
            continue
        if re.match(r'^[\d\s.,;\-–—/]+$', stripped):
            continue
        if _PHONE_RE.search(stripped):
            continue
        kept.append(stripped)
    result = '\n'.join(kept)
    result = re.sub(r'^[A-Za-z0-9.\s\-–—_]+(?=[\u4e00-\u9fff])',
                    '', result, count=1)
    return result


def _build_structured_summary(hits: list[dict], full_text: str,
                              fallback_snippet: str = "") -> str:
    """Extract budget, time, requirements from full text."""
    parts: list[str] = []
    if full_text:
        clean = _clean_page_text(full_text)
        m = re.search(r'预算(?:金额|总金额)?[：:]\s*(\d[\d.,]*\s*万?\s*元?)',
                      clean)
        if m:
            parts.append(f"预算：{m.group(1)}")
        m = re.search(
            r'(?:预计|拟|计划)采购时间[：:]\s*(\d{4}年\d{1,2}[月日]?)', clean)
        if m:
            parts.append(f"时间：{m.group(1)}")
        m = re.search(
            r'(?:采购(?:需求|内容|概况)|需采购|主要采购)[：:]?\s*'
            r'(.+?)(?:[。；;]|\n|$)', clean)
        if m:
            req = m.group(1).strip()
            if len(req) > 8:
                parts.append(req[:120])
        if len(parts) < 2:
            compact = re.sub(r'\s+', ' ', clean).strip()
            if len(compact) > 20:
                parts.append(compact[:150])

    if not parts:
        snippet = fallback_snippet or _pick_best_snippet(hits)
        if snippet:
            parts.append(snippet[:150])
    return " | ".join(parts) if parts else ""


def _extract_project_title(text: str) -> str:
    """Extract project name from text using known patterns."""
    for pat in _TITLE_PATTERNS:
        m = pat.search(text)
        if m:
            title = m.group(1).strip()
            title = re.sub(r'^[：:，,、。；;\s]+', '', title)
            title = re.sub(r'^[A-Za-z0-9.\s\-–—_]+(?=[\u4e00-\u9fff])',
                           '', title)
            title = re.sub(r'[：:，,、。；;\s]+$', '', title)
            if len(title) >= 8:
                return title
    return ""


def _make_entry_title(raw_title: str, url: str, summary: str = "") -> str:
    """Smart title: prefer project name from summary; fall back to page
    title only when not a bare site name; last resort is URL path."""
    from urllib.parse import urlparse

    if summary:
        title = _extract_project_title(summary)
        if title:
            return title[:60]

    stripped = raw_title.strip()
    if stripped and len(stripped) >= 6 and not _SITE_NAME_RE.search(stripped):
        return stripped[:80]

    path = urlparse(url).path.strip("/")
    if path:
        segments = path.split("/")
        last = segments[-1]
        if "." in last:
            last = last.rsplit(".", 1)[0]
        if len(last) >= 4:
            return last[:80]
    return stripped or url[:80]


async def _generate_job_ai_summary(keywords: list[str],
                                   new_results: list[dict]) -> dict | None:
    """Call AI to generate structured overview + entries for new job hits."""
    if not new_results:
        return None
    try:
        from ai import summarize_prompt, chat, parse_ai_entries
        messages = summarize_prompt(keywords, new_results)
        overview = await chat(messages, max_tokens=3000)
        overview = overview.strip()

        ai_overview, entries = parse_ai_entries(overview)
        return {"overview": ai_overview or overview, "entries": entries}
    except Exception:
        return None


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
        mode = job.get("render", "auto")
        archive_mode = mode == "archive"
        search_started = time.monotonic()
        budget = make_search_budget(
            job["startUrl"],
            archive_mode,
            search_started,
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
                    depth=3,
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
                    depth=3,
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
                            depth=3,
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
                    3,
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

        # ── 时间窗口：只统计上次运行至今的新增条目 ──
        last_run_at = _parse_dt(job.get("lastRunAt"))
        is_baseline = job.get("lastResult") is None
        if not is_baseline and last_run_at is not None:
            time_results = _filter_results_by_time(results, last_run_at)
        else:
            time_results = results
        new_hits = None if is_baseline else len(hit_keys(time_results) - prev)

        # ── AI 概括：非基线运行且存在新增命中时生成 ──
        ai_summary = None
        if new_hits is not None and new_hits > 0:
            new_results = _filter_new_hit_results(time_results, prev)
            overview_result = await _generate_job_ai_summary(
                all_keywords, new_results)
            if overview_result:
                entries = _build_job_summary_entries(
                    new_results, crawl_result.pages)
                ai_summary = {
                    "overview": overview_result["overview"],
                    "entries": entries,
                }

        summary = {
            "ranAt": datetime.now().isoformat(timespec="seconds"),
            "pagesCrawled": len(crawl_result.pages),
            "totalHits": total_hits,
            "newHits": new_hits,
            "renderUsed": render_used,
            "top": [{"pageUrl": r["pageUrl"], "pageTitle": r["pageTitle"],
                     "hitCount": len(r["hits"])} for r in results[:5]],
        }
        if ai_summary:
            summary["aiSummary"] = ai_summary
        job["lastResult"] = summary
        job["lastError"] = None
        job["lastRunAt"] = summary["ranAt"]
        job["prevKeys"] = sorted(keys)[:MAX_HIT_KEYS]
        return summary
    finally:
        job["running"] = False
        store.update(job)
