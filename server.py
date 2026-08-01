"""FastAPI app: serves the frontend and the /api/search endpoint."""
from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

def proactor_loop_factory():
    """uvicorn 自定义 loop factory（loop="server:proactor_loop_factory"）。
    自定义 factory 会被 asyncio.Runner 以零参数直接调用，须返回事件循环
    实例。Windows 的 reload/worker 子进程里 uvicorn 默认改用
    SelectorEventLoop，它不支持 asyncio 子进程（Playwright 启动浏览器
    需要），这里始终返回 ProactorEventLoop 实例。"""
    if sys.platform == "win32":
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from crawler import (PAGE_TIMEOUT, USER_AGENT, CrawlResult, crawl,
                     crawl_archive, describe_error, fetch_html)
from ai import (AIError, ask_prompt, chat_stream, expand_keywords,
                parse_ai_entries, summarize_prompt)
from cache import get as cache_get
from cache import put as cache_put
from discovery import (
    BudgetManager,
    DiscoveryRun,
    DiscoveryStats,
    discover_pages,
)
from locator import build_locate_page
from matcher import (
    extract_main_text,
    looks_js_driven,
    match_body_crawl_result,
    match_body_with_recall,
)
from search_budget import (
    BASE_BFS_PAGE_BUDGET,
    make_search_budget,
)

AUTO_LOW_HITS = 3
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="站内关键词搜索工具")


class SearchRequest(BaseModel):
    startUrl: str
    keywords: list[str] | str
    render: str = "auto"

    @field_validator("render")
    @classmethod
    def check_render(cls, v: str) -> str:
        if v not in ("auto", "on", "off", "archive"):
            raise ValueError("render 必须是 auto / on / off / archive")
        return v

    @field_validator("startUrl")
    @classmethod
    def check_url(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("起始 URL 必须是合法的 http/https 地址")
        return v

    @field_validator("keywords")
    @classmethod
    def check_keywords(cls, v: list[str] | str) -> list[str]:
        if isinstance(v, str):
            v = v.split()
        v = [k.strip() for k in v if k.strip()]
        if not v:
            raise ValueError("至少需要一个关键词")
        return v


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def _crawl_or_502(url: str, depth: int, render: bool,
                        deadline: float,
                        budget: BudgetManager | None = None) -> CrawlResult:
    try:
        result = await crawl(
            url,
            depth=depth,
            max_pages=BASE_BFS_PAGE_BUDGET,
            render=render,
            deadline=deadline,
            budget=budget,
        )
        if not result.pages:
            reason = (
                result.failed[0]["reason"]
                if result.failed
                else "未获取到起始页"
            )
            raise HTTPException(502, f"起始页无法访问：{reason}")
        return result
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502, f"起始页返回 HTTP {exc.response.status_code}，无法搜索")
    except httpx.TimeoutException:
        raise HTTPException(502, "起始页访问超时，无法搜索")
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, f"起始页无法访问：{describe_error(exc)}")


def _match_crawl(crawl_result, all_keywords: list[str]) -> tuple[list, int]:
    return match_body_crawl_result(crawl_result.pages, all_keywords)


def _search_budget(
    start_url: str,
    archive_mode: bool,
    started_at: float,
) -> BudgetManager:
    return make_search_budget(
        start_url,
        archive_mode,
        started_at,
    )


@app.post("/api/search")
async def search(req: SearchRequest) -> dict:
    search_started = time.monotonic()
    archive_mode = req.render == "archive"
    budget = _search_budget(
        req.startUrl,
        archive_mode,
        search_started,
    )
    deadline = budget.deadline
    host = urlsplit(req.startUrl).netloc
    try:
        async with asyncio.timeout(30):
            expanded = await expand_keywords(req.keywords, host)
    except TimeoutError:
        expanded = []
    expanded = _filter_off_topic(req.keywords, expanded)
    all_keywords = req.keywords + [
        k for k in expanded if k not in req.keywords]

    render_used = False
    auto_note = None
    if archive_mode:
        try:
            crawl_result = await crawl_archive(
                req.startUrl, deadline=deadline, budget=budget)
        except ValueError as exc:
            raise HTTPException(502, f"起始页无法访问：{exc}")
        if not crawl_result.pages:
            raise HTTPException(502, "起始页无法访问，无法归档深扫")
        render_used = True
        auto_note = "归档深扫：已全量翻页栏目列表并抓取全部文章正文"
    elif req.render == "on":
        crawl_result = await _crawl_or_502(
            req.startUrl, 3, True, deadline, budget)
        render_used = True
    else:
        crawl_result = await _crawl_or_502(
            req.startUrl, 3, False, deadline, budget)
        if req.render == "auto" and crawl_result.pages:
            _, static_hits = _match_crawl(crawl_result, all_keywords)
            js_suspect = looks_js_driven(crawl_result.pages[0].html)
            if static_hits == 0 or (js_suspect
                                    and static_hits <= AUTO_LOW_HITS):
                try:
                    render_result = await _crawl_or_502(
                        req.startUrl, 3, True, deadline, budget)
                except HTTPException:
                    auto_note = "静态结果可能不完整，自动渲染补搜失败"
                else:
                    _, render_hits = _match_crawl(render_result, all_keywords)
                    if render_hits > static_hits:
                        crawl_result = render_result
                        render_used = True
                        auto_note = "静态抓取不完整，已自动启用 JS 渲染补搜"
                    else:
                        auto_note = "已自动尝试渲染补搜，未发现更多结果"

    if archive_mode:
        # 归档深扫已做全量发现（渲染全翻页+全部正文），跳过 discovery，
        # 否则 discovery 会在剩余的大预算里重复翻页爬行，浪费数分钟
        discovery_run = DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(
                profile="archive",
                elapsed_ms=max(
                    0,
                    int((time.monotonic() - search_started) * 1000),
                ),
            ),
        )
    else:
        try:
            discovery_run = await discover_pages(
                req.startUrl,
                all_keywords,
                crawl_result,
                3,
                req.render,
                budget=budget,
            )
        except Exception as exc:
            discovery_run = DiscoveryRun(
                pages=[],
                failed=[],
                stats=DiscoveryStats(
                    profile="generic",
                    partial=True,
                    elapsed_ms=max(
                        0,
                        int(
                            (time.monotonic() - search_started)
                            * 1000
                        ),
                    ),
                    warnings=[
                        f"discovery: {type(exc).__name__}"
                    ],
                ),
            )
    crawl_result.pages.extend(discovery_run.pages)
    crawl_result.failed.extend(discovery_run.failed)

    if discovery_run.stats.profile == "freecms":
        results, total_hits, weak_hits = match_body_with_recall(
            crawl_result.pages,
            all_keywords,
            discovery_run.candidates,
        )
    else:
        results, total_hits = _match_crawl(crawl_result, all_keywords)
        for result in results:
            result["matchStrength"] = "strong"
        weak_hits = 0
    discovery_run.stats.weak_candidates = weak_hits
    response = {
        "startUrl": req.startUrl,
        "keywords": req.keywords,
        "expandedKeywords": expanded,
        "depth": 3,
        "render": render_used,
        "renderMode": req.render,
        "autoNote": auto_note,
        "discovery": discovery_run.stats.as_dict(),
        "siteSearch": {
            "available": (
                "site-search"
                in discovery_run.stats.sources_succeeded
            ),
            "linksFound": discovery_run.stats.candidates_found,
            "pagesFetched": discovery_run.stats.candidates_fetched,
            "deprecated": True,
        },
        "pagesCrawled": len(crawl_result.pages),
        "crawledPages": [p.url for p in crawl_result.pages],
        "pagesFailed": crawl_result.failed,
        "totalHits": total_hits,
        "weakHits": weak_hits,
        "results": results,
    }
    texts = {
        p.url: extract_main_text(p.html)
        for p in crawl_result.pages
    }
    response["searchId"] = cache_put(response, texts)
    return response


@app.get("/api/locate")
async def locate(url: str, keyword: str) -> HTMLResponse:
    """Return a sanitized copy of `url` with obvious keyword highlighting.

    For yngp.com: warms the session (GET list page + POST district API)
    so the detail page request passes the server's anti-hotlinking check.
    """
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(422, "url 必须是合法的 http/https 地址")
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(422, "keyword 不能为空")
    origin = f"{parts.scheme}://{parts.netloc}"
    hostname = (parts.hostname or "").lower().rstrip(".")
    referer = f"{origin}/"
    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": referer,
            **({"Origin": origin} if hostname == "yngp.com" or hostname.endswith(".yngp.com") else {}),
        },
    ) as client:
        # yngp.com 需要先建立会话（访问列表页 + 调行政区划接口），
        # 否则详情页返回"请验证后再访问"
        if hostname == "yngp.com" or hostname.endswith(".yngp.com"):
            try:
                await client.get(
                    origin + "/page/procurement/procurementList.html",
                )
                await client.post(
                    origin + "/api/common/otheruse.getdistrictlist.svc",
                    content="",
                    headers={
                        "Origin": origin,
                        "Referer": origin + "/page/procurement/procurementList.html",
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    },
                )
            except Exception:
                pass
        try:
            html = await fetch_html(client, url)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                502, f"目标页返回 HTTP {exc.response.status_code}")
        except (httpx.RequestError, ValueError) as exc:
            raise HTTPException(502, f"目标页无法访问：{describe_error(exc)}")
    return HTMLResponse(build_locate_page(html, url, keyword))


def sse_event(payload: dict) -> bytes:
    import json as _json
    return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def sse_stream(messages: list[dict]):
    try:
        async for delta in chat_stream(messages):
            yield sse_event({"type": "delta", "text": delta})
        yield sse_event({"type": "done"})
    except AIError as exc:
        yield sse_event({"type": "error", "message": str(exc)})


def _filter_off_topic(original: list[str], expanded: list[str]) -> list[str]:
    """Remove expanded keywords that diverge into unrelated domains.
    An expanded keyword must share at least one Chinese character with
    at least one original keyword, or be a location-variant of the search."""
    if not expanded:
        return []
    # 收集原始关键词的所有中文字符
    orig_chars: set[str] = set()
    for k in original:
        orig_chars.update(re.findall(r'[\u4e00-\u9fff]', k))

    if not orig_chars:
        return expanded  # 纯英文搜索，不过滤

    kept: list[str] = []
    for k in expanded:
        k_chars = set(re.findall(r'[\u4e00-\u9fff]', k))
        # 必须与原始关键词有至少 1 个共同中文字符
        if k_chars & orig_chars:
            kept.append(k)
    return kept


def _build_entries_from_pages(pages: list[dict], texts: dict[str, str]) -> list[dict]:
    """Build entry cards from the structured search-result page data."""
    from urllib.parse import urlparse

    seen: set[str] = set()
    entries: list[dict] = []
    for p in pages:
        url = p.get("pageUrl", "")
        if not url or url in seen:
            continue
        seen.add(url)
        hits = p.get("hits", [])
        if not hits:
            continue
        # 过滤：至少有一个命中的关键词不是过于宽泛的短词
        hit_kws = {h.get("keyword", "") for h in hits}
        specific = [k for k in hit_kws if len(k) >= 3 or re.search(r'[\u4e00-\u9fff]{2,}', k)]
        if not specific:
            continue
        full_text = texts.get(url, "")
        summary = _build_structured_summary(hits, full_text)
        if not summary:
            continue
        raw_title = p.get("pageTitle") or ""
        title = _make_entry_title(raw_title, url, summary)
        date = p.get("publishedDate", "")
        entries.append(dict(
            date=date, title=title, summary=summary,
            link=url,
        ))
    entries.sort(key=lambda e: e["date"] or "", reverse=True)
    return entries


_PROJECT_TERMS = re.compile(r'项目|采购|招标|中标|成交|合同|工程|服务|设备|施工|监理|设计|公告|公示')
# 干扰项：客服电话、技术支持、联系方式等
_NOISE_TERMS = re.compile(r'(?:电话|咨询|支持|联系|投诉|监督|地址|邮编|传真|邮箱|客服|热线|工作日|办理|CA|数字证书|登录|注册|密码)')


def _pick_best_snippet(hits: list[dict]) -> str:
    """Select the most informative snippet from all keyword hits on a page,
    penalizing contact/support boilerplate."""
    best = ""
    best_score = 0
    for h in hits:
        s = h.get("snippet", "").strip()
        if not s:
            continue
        score = min(len(s), 200)
        if _PROJECT_TERMS.search(s):
            score += 100
        # 重罚干扰项："电话"、"技术支持"、"CA数字证书" 等
        if _NOISE_TERMS.search(s):
            score -= 200
        if score > best_score:
            best_score = score
            best = s
    return best[:200] if best and best_score > 0 else ""


# 结构化摘要提取正则
_BUDGET_RE = re.compile(r'预算(?:金额|总金额)?[：:]\s*(\d[\d.,]*\s*万?\s*元?)')
_TIME_RE = re.compile(r'(?:预计|拟|计划)采购时间[：:]\s*(\d{4}年\d{1,2}[月日]?)')
_REQUIRE_RE = re.compile(
    r'(?:采购(?:需求|内容|概况)|需采购|主要采购)[：:]?\s*(.+?)(?:[。；;]|\n|$)')
_PHONE_RE = re.compile(r'\b\d{3,4}[-–—]\d{7,8}\b')


def _clean_page_text(text: str) -> str:
    """Strip header/footer noise lines common in Chinese government sites."""
    lines = text.split('\n')
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # 丢弃纯导航关键词短行
        if len(stripped) < 12 and re.search(
            r'^(?:首页|网站|联系|关于|设为|加入|收藏|主办|承办|技术支持|'
            r'建议|分辨率|浏览器|当前位置|今天是|欢迎|访问|进入|返回|登录|'
            r'注册|版权所有|All Rights|Copyright|ICP|备案)', stripped):
            continue
        # 丢弃联系方式/客服行
        if re.search(r'(?:技术支持|咨询电话|服务热线|监督电话|投诉电话|'
                     r'CA\s*数字证书|办理地点|工作日|上午|下午|邮箱|传真|邮编)', stripped):
            continue
        # 丢弃纯数字/编号行
        if re.match(r'^[\d\s.,;\-–—/]+$', stripped):
            continue
        # 丢弃电话号码行
        if _PHONE_RE.search(stripped):
            continue
        kept.append(stripped)
    result = '\n'.join(kept)
    # 开头剥离字母数字+点号噪音前缀（如 "bee97.19e0bc.1da3 cgyxgk"）
    result = re.sub(r'^[A-Za-z0-9.\s\-–—_]+(?=[\u4e00-\u9fff])', '', result, count=1)
    return result


def _build_structured_summary(hits: list[dict], full_text: str) -> str:
    """Extract budget, time, and requirements from full page text.
    Falls back to full-text excerpt, then best snippet as last resort."""
    parts: list[str] = []

    if full_text:
        full_text = _clean_page_text(full_text)

        m = _BUDGET_RE.search(full_text)
        if m:
            parts.append(f"预算：{m.group(1)}")

        m = _TIME_RE.search(full_text)
        if m:
            parts.append(f"时间：{m.group(1)}")

        m = _REQUIRE_RE.search(full_text)
        if m:
            req = m.group(1).strip()
            if len(req) > 8:
                parts.append(req[:120])

        # 结构化字段不足 → 取全文前 150 字（已清洗）
        if len(parts) < 2:
            clean = re.sub(r'\s+', ' ', full_text).strip()
            if len(clean) > 20:
                parts.append(clean[:150])

    # 最后兜底：snippet（已过噪声过滤）
    if not parts:
        snippet = _pick_best_snippet(hits)
        if snippet:
            parts.append(snippet[:150])

    return " | ".join(parts) if parts else ""


def _make_entry_title(raw_title: str, url: str, summary: str = "") -> str:
    """Extract project/school name from snippet by locating project-related markers,
    falling back to page title or URL path."""
    from urllib.parse import urlparse

    if summary:
        title = _extract_project_title(summary)
        if title:
            return title[:60]

    # 原始标题有实质内容且不是站点名才直接用
    stripped = raw_title.strip()
    if stripped and len(stripped) >= 6 and not _SITE_NAME_RE.search(stripped):
        return stripped[:80]

    # 从 URL 路径提取最后一段
    path = urlparse(url).path.strip("/")
    if path:
        segments = path.split("/")
        last = segments[-1]
        if "." in last:
            last = last.rsplit(".", 1)[0]
        if len(last) >= 4:
            return last[:80]
    return stripped or url[:80]


# 项目标题提取正则：按优先级排列
_TITLE_PATTERNS = [
    # "采购项目名称：xxx" / "项目名称：xxx"
    re.compile(r'(?:采购)?项目名称[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    # "项目编号：xxx  项目名称：xxx"
    re.compile(r'项目编号[：:][^。；;\n]*?项目名称[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    # "采购意向：xxx" / "采购需求：xxx"
    re.compile(r'采购(?:意向|需求|内容)[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    # "XXX项目" / "XXX采购" / "XXX招标" — 往前取到标点或行首
    re.compile(r'(?:^|[。；;，,、\n])\s*([^。；;，,、\n]{10,60}?(?:项目|采购项目|招标项目|中标|成交|合同))'),
    # "项目概况\nxxx" — 取下一行
    re.compile(r'项目概况[：:]?\s*\n?\s*(.+?)(?:[。；;]|\n|$)'),
    # 兜底：取包含"项目/采购/招标"的前后文
    re.compile(r'(.{8,50}(?:项目|采购|招标|中标|施工|监理|设备).{0,30})'),
]

# 站点名标题：形如"XX政府采购网"、"XX人民政府"等，不能当文章标题用
_SITE_NAME_RE = re.compile(
    r'(?:政府采购网|采购网|招标网|公共资源|政府门户|人民政府|'
    r'政务服务平台|政务服务网|交易平台|交易中心|'
    r'信息网|服务网|门户网站)$')


def _extract_project_title(text: str) -> str:
    """Try to extract a project name from snippet text using known patterns."""
    for pat in _TITLE_PATTERNS:
        m = pat.search(text)
        if m:
            title = m.group(1).strip()
            # 去除常见无意义前缀
            title = re.sub(r'^[：:，,、。；;\s]+', '', title)
            # 去除字母数字+点号+空格前缀（如 "bee97.19e0bc.1da3 cgyxgk"）
            title = re.sub(r'^[A-Za-z0-9.\s\-–—_]+(?=[\u4e00-\u9fff])', '', title)
            title = re.sub(r'[：:，,、。；;\s]+$', '', title)
            if len(title) >= 8:
                return title
    return ""


async def sse_summarize(messages: list[dict], pages: list[dict], texts: dict[str, str]):
    """Stream AI deltas, then parse AI-structured entries (fall back to regex)."""
    full_text = ""
    try:
        async for delta in chat_stream(messages):
            full_text += delta
            yield sse_event({"type": "delta", "text": delta})

        ai_overview, ai_entries = parse_ai_entries(full_text)
        if ai_entries:
            overview = ai_overview or full_text.strip()
            entries = ai_entries
        else:
            overview = full_text.strip()
            entries = _build_entries_from_pages(pages, texts)

        yield sse_event({
            "type": "parsed",
            "overview": overview,
            "entries": entries,
        })
        yield sse_event({"type": "done"})
    except AIError as exc:
        yield sse_event({"type": "error", "message": str(exc)})


class AIRequest(BaseModel):
    searchId: str


class AskRequest(BaseModel):
    searchId: str
    question: str

    @field_validator("question")
    @classmethod
    def check_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("问题不能为空")
        return v


@app.on_event("shutdown")
async def _close_browser() -> None:
    import renderer
    await renderer.shutdown()


# ---------- 定时搜索任务 ----------

from datetime import datetime  # noqa: E402

from jobs import (JobStore, describe_schedule, is_due, make_job,  # noqa: E402
                  next_run_at, run_job, validate_schedule)

SCHEDULER_TICK_SECONDS = 30
job_store = JobStore()


def _job_view(job: dict) -> dict:
    view = dict(job)
    view["scheduleText"] = describe_schedule(job["schedule"])
    view["nextRun"] = next_run_at(job, datetime.now())
    return view


class JobRequest(SearchRequest):
    schedule: dict
    name: str = ""


@app.get("/api/jobs")
async def list_jobs() -> dict:
    return {"jobs": [_job_view(j) for j in job_store.list()]}


@app.post("/api/jobs")
async def create_job(req: JobRequest) -> dict:
    try:
        schedule = validate_schedule(req.schedule)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    job = make_job(req.startUrl, req.keywords, req.render,
                   schedule, name=req.name.strip())
    job_store.add(job)
    return {"job": _job_view(job)}


@app.post("/api/jobs/{job_id}/toggle")
async def toggle_job(job_id: str) -> dict:
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    job["enabled"] = not job.get("enabled", True)
    job_store.update(job)
    return {"enabled": job["enabled"]}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    if not job_store.remove(job_id):
        raise HTTPException(404, "任务不存在")
    return {"deleted": True}


@app.post("/api/jobs/{job_id}/run")
async def run_job_now(job_id: str) -> dict:
    if not job_store.get(job_id):
        raise HTTPException(404, "任务不存在")
    asyncio.create_task(run_job(job_store, job_id))
    return {"started": True}


async def scheduler_loop() -> None:
    """每 30 秒扫描一次任务表，顺序执行到期任务。"""
    while True:
        try:
            now = datetime.now()
            for job in job_store.list():
                if is_due(job, now) and not job.get("running"):
                    await run_job(job_store, job["id"])
        except Exception:  # noqa: BLE001 - 调度循环不能死
            pass
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)


@app.on_event("startup")
async def _start_scheduler() -> None:
    asyncio.create_task(scheduler_loop())


@app.post("/api/summarize")
async def summarize(req: AIRequest) -> StreamingResponse:
    entry = cache_get(req.searchId)
    if not entry:
        raise HTTPException(404, "搜索结果不存在或已过期，请重新搜索")
    result = entry["result"]
    all_kw = result["keywords"] + result.get("expandedKeywords", [])
    messages = summarize_prompt(all_kw, result["results"])
    return StreamingResponse(sse_summarize(messages, result["results"], entry["texts"]),
                             media_type="text/event-stream")


@app.post("/api/ask")
async def ask(req: AskRequest) -> StreamingResponse:
    entry = cache_get(req.searchId)
    if not entry:
        raise HTTPException(404, "搜索结果不存在或已过期，请重新搜索")
    result = entry["result"]
    pages = [{"url": p["pageUrl"], "title": p["pageTitle"],
              "text": entry["texts"].get(p["pageUrl"], "")}
             for p in result["results"]]
    messages = ask_prompt(result["keywords"], req.question, pages)
    return StreamingResponse(sse_stream(messages),
                             media_type="text/event-stream")


if __name__ == "__main__":
    import argparse
    import os
    import sys

    # 预览环境可能用系统/托管 Python 启动（缺 playwright 等依赖）；
    # 若项目 .venv 存在则切换到它，保证渲染模式可用
    _venv_py = Path(__file__).parent / ".venv" / "Scripts" / "python.exe"
    if (_venv_py.exists()
            and Path(sys.executable).resolve() != _venv_py.resolve()):
        os.execv(str(_venv_py),
                 [str(_venv_py), str(Path(__file__).resolve()),
                  *sys.argv[1:]])

    parser = argparse.ArgumentParser(description="站内关键词搜索工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7100)
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="单进程运行（不做文件变更自动重启），停止时不留 worker 进程",
    )
    args = parser.parse_args()
    # reload=True: dev server auto-restarts when Python files change,
    # so the preview never serves stale backend code; --no-reload keeps one
    # process for the launcher so its process tree can be stopped cleanly.
    uvicorn.run("server:app", host=args.host, port=args.port,
                reload=not args.no_reload,
                loop="server:proactor_loop_factory")
