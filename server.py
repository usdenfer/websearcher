"""FastAPI app: serves the frontend and the /api/search endpoint."""
from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
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
                     describe_error, fetch_html)
from ai import (AIError, ask_prompt, chat_stream, expand_keywords,
                summarize_prompt)
from cache import get as cache_get
from cache import put as cache_put
from locator import build_locate_page
from matcher import (extract_text, extract_title, looks_js_driven,
                     match_crawl_result, match_page)

SEARCH_BUDGET_SECONDS = 120
RENDER_BUDGET_SECONDS = 600
SITESEARCH_BUDGET_SECONDS = 45
AUTO_LOW_HITS = 3
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="站内关键词搜索工具")


class SearchRequest(BaseModel):
    startUrl: str
    keywords: list[str] | str
    depth: int = 1
    render: str = "auto"

    @field_validator("render")
    @classmethod
    def check_render(cls, v: str) -> str:
        if v not in ("auto", "on", "off"):
            raise ValueError("render 必须是 auto / on / off")
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

    @field_validator("depth")
    @classmethod
    def check_depth(cls, v: int) -> int:
        if not 1 <= v <= 3:
            raise ValueError("搜索深度必须在 1~3 层之间")
        return v


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def _crawl_or_502(url: str, depth: int, render: bool,
                        budget: int) -> CrawlResult:
    try:
        async with asyncio.timeout(budget):
            return await crawl(url, depth=depth, render=render)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502, f"起始页返回 HTTP {exc.response.status_code}，无法搜索")
    except httpx.TimeoutException:
        raise HTTPException(502, "起始页访问超时，无法搜索")
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, f"起始页无法访问：{describe_error(exc)}")
    except TimeoutError:
        raise HTTPException(504, "搜索总耗时超过上限，请缩小范围或改用静态模式")


def _match_crawl(crawl_result, all_keywords: list[str]) -> tuple[list, int]:
    return match_crawl_result(crawl_result.pages, all_keywords)


@app.post("/api/search")
async def search(req: SearchRequest) -> dict:
    host = urlsplit(req.startUrl).netloc
    expanded = await expand_keywords(req.keywords, host)
    all_keywords = req.keywords + [
        k for k in expanded if k not in req.keywords]

    render_used = False
    auto_note = None
    if req.render == "on":
        crawl_result = await _crawl_or_502(
            req.startUrl, req.depth, True, RENDER_BUDGET_SECONDS)
        render_used = True
    else:
        crawl_result = await _crawl_or_502(
            req.startUrl, req.depth, False, SEARCH_BUDGET_SECONDS)
        if req.render == "auto" and crawl_result.pages:
            _, static_hits = _match_crawl(crawl_result, all_keywords)
            js_suspect = looks_js_driven(crawl_result.pages[0].html)
            if static_hits == 0 or (js_suspect
                                    and static_hits <= AUTO_LOW_HITS):
                try:
                    render_result = await _crawl_or_502(
                        req.startUrl, req.depth, True, RENDER_BUDGET_SECONDS)
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

    site_search_info = {"available": False, "linksFound": 0,
                        "pagesFetched": 0}
    try:
        async with asyncio.timeout(SITESEARCH_BUDGET_SECONDS):
            import sitesearch
            extra_pages, site_search_info = await sitesearch.collect_pages(
                req.startUrl, all_keywords,
                skip={p.url for p in crawl_result.pages})
        crawl_result.pages.extend(extra_pages)
    except Exception:  # noqa: BLE001 - 站内搜索只是补充通道，失败忽略
        pass

    results, total_hits = _match_crawl(crawl_result, all_keywords)
    response = {
        "startUrl": req.startUrl,
        "keywords": req.keywords,
        "expandedKeywords": expanded,
        "depth": req.depth,
        "render": render_used,
        "renderMode": req.render,
        "autoNote": auto_note,
        "siteSearch": site_search_info,
        "pagesCrawled": len(crawl_result.pages),
        "crawledPages": [p.url for p in crawl_result.pages],
        "pagesFailed": crawl_result.failed,
        "totalHits": total_hits,
        "results": results,
    }
    texts = {p.url: extract_text(p.html) for p in crawl_result.pages}
    response["searchId"] = cache_put(response, texts)
    return response


@app.get("/api/locate")
async def locate(url: str, keyword: str) -> HTMLResponse:
    """Return a sanitized copy of `url` with obvious keyword highlighting."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(422, "url 必须是合法的 http/https 地址")
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(422, "keyword 不能为空")
    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
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
    job = make_job(req.startUrl, req.keywords, req.depth, req.render,
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
    return StreamingResponse(sse_stream(messages),
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
    args = parser.parse_args()
    # reload=True: dev server auto-restarts when Python files change,
    # so the preview never serves stale backend code
    uvicorn.run("server:app", host=args.host, port=args.port, reload=True,
                loop="server:proactor_loop_factory")
