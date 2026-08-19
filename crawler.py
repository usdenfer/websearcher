"""Crawler: fetch start page, extract same-site links, fetch subpages concurrently."""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from discovery.urltools import (
    canonical_authority,
    normalize_candidate_url,
    same_site_boundary,
)
from matcher import extract_published_date

MAX_SUBPAGES = 5000
MAX_TOTAL_PAGES = 5000
RENDER_MAX_PAGES = 5000
RENDER_SUBPAGE_LINKS = 5000
RENDER_DISCOVERY_HUBS = 2
RENDER_DISCOVERY_ARTICLES_PER_HUB = 24
CONCURRENCY = int(os.environ.get("BFS_CONCURRENCY", "6"))
PAGE_TIMEOUT = 15.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BINARY_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "bmp",
    "pdf", "zip", "rar", "7z", "tar", "gz",
    "mp4", "mp3", "avi", "mov", "webm", "wav",
    "css", "js", "json", "xml", "woff", "woff2", "ttf", "eot",
}


@dataclass
class CrawledPage:
    url: str
    html: str


@dataclass(frozen=True)
class FetchedHtml:
    html: str
    final_url: str


@dataclass(frozen=True)
class _CrawlHttpContext:
    client: httpx.AsyncClient
    redirect_allowed: Callable[[str], bool]
    reserve_request: Callable[[], bool] | None = None


class UnsafeRedirect(ValueError):
    """A crawl redirect attempted to leave its permitted site boundary."""


class PageBudgetExhausted(RuntimeError):
    """A main-document request was refused by the shared page budget."""


@dataclass
class CrawlResult:
    pages: list[CrawledPage] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


def normalize_url(url: str) -> str:
    return normalize_candidate_url(url)


def is_binary_url(url: str) -> bool:
    filename = urlsplit(url).path.rsplit("/", 1)[-1]
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[-1].lower() in BINARY_EXTENSIONS


_INDEX_NAMES = {"index.shtml", "index.html", "index.htm"}
_ARTICLE_SUFFIXES = (".shtml", ".html", ".htm")


def is_render_discovery_hub(url: str) -> bool:
    """Return whether a rendered link is likely to lead to more content."""
    parts = urlsplit(url)
    filename = parts.path.rstrip("/").rsplit("/", 1)[-1].lower()
    return filename in _INDEX_NAMES or (
        bool(parts.query) and bool(filename) and "." not in filename)


def is_render_article_link(url: str) -> bool:
    """Return whether a rendered link is an HTML article rather than an index."""
    filename = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    return filename.endswith(_ARTICLE_SUFFIXES) and filename not in _INDEX_NAMES


def prioritize_render_links(links: list[str]) -> list[str]:
    """Order rendered links so discovery hubs are crawled before articles."""
    def rank(link: str) -> int:
        parts = urlsplit(link)
        filename = parts.path.rstrip("/").rsplit("/", 1)[-1].lower()
        if bool(parts.query) and bool(filename) and "." not in filename:
            return 0
        if filename in _INDEX_NAMES:
            return 1
        if is_render_article_link(link):
            return 2
        return 3

    return sorted(links, key=rank)


def extract_same_site_links(html: str, base_url: str,
                            limit: int = MAX_SUBPAGES,
                            skip: set[str] = frozenset()) -> list[str]:
    """Extract up to `limit` fresh same-site links from a page.

    URLs present in `skip` (e.g. already visited) are ignored before the
    limit is applied, so stale navigation links cannot consume the quota
    ahead of unseen content links.
    """
    base_authority = canonical_authority(base_url)
    base_norm = normalize_url(base_url)
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"])
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if canonical_authority(absolute) != base_authority:
            continue
        normalized = normalize_url(absolute)
        if normalized == base_norm or is_binary_url(normalized):
            continue
        if normalized in skip or normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= limit:
            break
    return links


def describe_error(exc: Exception) -> str:
    from renderer import RenderError
    if isinstance(exc, RenderError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "访问超时"
    if isinstance(exc, httpx.ConnectError):
        return "连接失败"
    if isinstance(exc, ValueError):
        return str(exc)
    return type(exc).__name__


async def fetch_html_response(
    client: httpx.AsyncClient,
    url: str,
    reserve_request: Callable[[], bool] | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> FetchedHtml:
    if reserve_request is None and redirect_allowed is None:
        response = await client.get(url)
        return _html_from_response(response)

    current_url = url
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        if reserve_request is not None and not reserve_request():
            raise PageBudgetExhausted("页面预算已用尽")
        response = await client.get(
            current_url, follow_redirects=False
        )
        if response.is_redirect and response.headers.get("location"):
            next_url = urljoin(
                str(response.url), response.headers["location"]
            )
            if (
                redirect_allowed is not None
                and not redirect_allowed(next_url)
            ):
                raise UnsafeRedirect("重定向到站外地址")
            current_url = next_url
            continue
        return _html_from_response(response)
    raise httpx.TooManyRedirects(
        "重定向次数过多",
        request=httpx.Request("GET", current_url),
    )


async def fetch_html(
    client: httpx.AsyncClient,
    url: str,
    reserve_request: Callable[[], bool] | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> str:
    """Fetch HTML while preserving the historical string return contract."""
    return (
        await fetch_html_response(
            client, url, reserve_request, redirect_allowed
        )
    ).html


RETRYABLE_TRANSPORT = (httpx.ConnectError, httpx.RemoteProtocolError,
                       httpx.ReadError, httpx.ConnectTimeout,
                       httpx.ReadTimeout, httpx.PoolTimeout)

MAX_REDIRECT_HOPS = 10


def _html_from_response(response: httpx.Response) -> FetchedHtml:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise ValueError(f"非 HTML 内容 ({content_type or '未知类型'})")
    return FetchedHtml(response.text, str(response.url))


async def _fetch_html_manual_redirect(
    context: _CrawlHttpContext,
    url: str,
) -> FetchedHtml:
    current_url = url
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        if (
            context.reserve_request is not None
            and not context.reserve_request()
        ):
            raise PageBudgetExhausted("页面预算已用尽")
        response = await context.client.get(
            current_url,
            follow_redirects=False,
        )
        if response.is_redirect and response.headers.get("location"):
            next_url = urljoin(
                str(response.url),
                response.headers["location"],
            )
            if not context.redirect_allowed(next_url):
                raise UnsafeRedirect("重定向到站外地址")
            current_url = next_url
            continue
        return _html_from_response(response)
    raise httpx.TooManyRedirects(
        "重定向次数过多",
        request=httpx.Request("GET", current_url),
    )


async def _fetch_crawl_html_retry(
    client: _CrawlHttpContext,
    url: str,
    attempts: int = 6,
    base_delay: float = 1.5,
) -> FetchedHtml:
    """Crawl-only retry path with redirect validation before each next hop."""
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await _fetch_html_manual_redirect(client, url)
        except RETRYABLE_TRANSPORT as exc:
            last_exc = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_exc = exc
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
            delay *= 2
    raise last_exc  # type: ignore[misc]


async def fetch_html_retry(client: httpx.AsyncClient, url: str,
                           attempts: int = 6,
                           base_delay: float = 1.5,
                           include_final_url: bool = False,
                           reserve_request: Callable[[], bool] | None = None,
                           redirect_allowed: Callable[[str], bool] | None = None,
                           ) -> str | FetchedHtml:
    """带重试的页面抓取：政府站 WAF 常间歇性拒连/掐断响应/偶发 5xx，
    瞬时失败用指数退避重试（1.5s/3s/6s），非瞬时错误（4xx、非 HTML）
    不重试直接抛出。"""
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            if include_final_url:
                return await fetch_html_response(
                    client, url, reserve_request, redirect_allowed
                )
            return await fetch_html(
                client, url, reserve_request, redirect_allowed
            )
        except RETRYABLE_TRANSPORT as exc:
            last_exc = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_exc = exc
        if attempt < attempts - 1:
            await asyncio.sleep(delay)
            delay *= 2
    raise last_exc  # type: ignore[misc]


_ORIGINAL_FETCH_HTML_RETRY = fetch_html_retry


def _as_fetched_html(
    value: str | FetchedHtml,
    requested_url: str,
) -> FetchedHtml:
    """Accept old string-returning test doubles without changing public APIs."""
    if isinstance(value, FetchedHtml):
        return value
    return FetchedHtml(value, requested_url)


async def _fetch_crawl_html(
    context: _CrawlHttpContext,
    url: str,
    attempts: int,
    base_delay: float,
) -> FetchedHtml:
    """Honor legacy public test patches; otherwise use the secure crawl path."""
    if fetch_html_retry is not _ORIGINAL_FETCH_HTML_RETRY:
        if (
            context.reserve_request is not None
            and not context.reserve_request()
        ):
            raise PageBudgetExhausted("页面预算已用尽")
        value = await fetch_html_retry(
            context.client,
            url,
            attempts,
            base_delay,
        )
        return _as_fetched_html(value, url)
    return await _fetch_crawl_html_retry(
        context,
        url,
        attempts,
        base_delay,
    )


async def gather_before_deadline(coroutines, deadline: float | None):
    """Gather completed work and cooperatively cancel work past a deadline."""
    tasks = [asyncio.create_task(item) for item in coroutines]
    if not tasks:
        return [], False
    timeout = (
        None
        if deadline is None
        else max(0.0, deadline - time.monotonic())
    )
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results = [
        task.result()
        for task in tasks
        if task in done
        and not task.cancelled()
        and task.exception() is None
    ]
    return results, bool(pending)


async def await_before_deadline(coroutine, deadline: float | None):
    """Await one operation, preserving its exception unless time runs out."""
    if deadline is not None and deadline <= time.monotonic():
        close = getattr(coroutine, "close", None)
        if close is not None:
            close()
        return None, True
    task = asyncio.create_task(coroutine)
    timeout = (
        None
        if deadline is None
        else max(0.0, deadline - time.monotonic())
    )
    try:
        done, pending = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if pending:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None, True
    return task.result(), False


async def crawl(start_url: str, depth: int = 1,
                max_pages: int = MAX_TOTAL_PAGES,
                render: bool = False,
                deadline: float | None = None,
                budget: Any | None = None,
                since: str | None = None) -> CrawlResult:
    """Fetch start page and its same-site pages level by level (BFS).

    depth=1 crawls the start page plus pages it directly links to;
    depth=2 additionally crawls links found on those pages, and so on.
    At most max_pages pages are fetched in total. Raises if the start
    page itself fails; other failures are collected into
    CrawlResult.failed.
    render=True 时用 headless Chromium 渲染每个页面（JS 动态站点），
    链接取自渲染后的 DOM（含翻页发现的链接），页数上限为
    RENDER_MAX_PAGES。
    since 为 YYYY-MM-DD 格式的时间下限，抓取后若页面发布日期已知且早于
    since 则跳过该页面、不继续跟踪其链接。
    """
    if render:
        kwargs = {"max_pages": max_pages, "deadline": deadline, "since": since}
        if budget is not None:
            kwargs["budget"] = budget
        return await _crawl_render(start_url, depth, **kwargs)
    result = CrawlResult()
    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        start_context = _CrawlHttpContext(
            client,
            lambda target: same_site_boundary(start_url, target),
            None if budget is None else budget.reserve_html,
        )
        try:
            start_value, deadline_reached = await await_before_deadline(
                _fetch_crawl_html(
                    start_context,
                    start_url,
                    4,
                    1.5,
                ),
                deadline,
            )
        except PageBudgetExhausted:
            result.failed.append({
                "url": start_url,
                "reason": "页面预算已用尽",
            })
            return result
        if deadline_reached:
            result.failed.append({
                "url": start_url,
                "reason": "搜索截止时间已到",
            })
            return result
        start_fetched = _as_fetched_html(start_value, start_url)
        start_page = CrawledPage(
            url=start_fetched.final_url,
            html=start_fetched.html,
        )
        result.pages.append(start_page)
        effective_authority = canonical_authority(start_page.url)
        if effective_authority is None:
            raise ValueError("重定向后的地址无效")
        child_context = _CrawlHttpContext(
            client,
            lambda target: canonical_authority(target) == effective_authority,
            None if budget is None else budget.reserve_html,
        )
        visited = {
            normalize_url(start_url),
            normalize_url(start_page.url),
        }
        stored_pages = {normalize_url(start_page.url)}
        attempted_pages = 1
        current_level = [start_page]
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def fetch_one(url: str) -> CrawledPage | None:
            async with semaphore:
                try:
                    value = await _fetch_crawl_html(
                        child_context,
                        url,
                        2,
                        1.0,
                    )
                except UnsafeRedirect:
                    result.failed.append({
                        "url": url,
                        "reason": "重定向到站外地址",
                    })
                    return None
                except PageBudgetExhausted:
                    result.failed.append({
                        "url": url,
                        "reason": "页面预算已用尽",
                    })
                    return None
                except Exception as exc:  # noqa: BLE001 - collected, not raised
                    result.failed.append(
                        {"url": url, "reason": describe_error(exc)})
                    return None
                fetched = _as_fetched_html(value, url)
                if since:
                    page_date = extract_published_date(
                        fetched.html, fetched.final_url)
                    if page_date and page_date < since:
                        return None
                return CrawledPage(
                    url=fetched.final_url,
                    html=fetched.html,
                )

        for _level in range(depth):
            remaining = max_pages - attempted_pages
            if remaining <= 0:
                break
            candidates: list[str] = []
            for page in current_level:
                for link in extract_same_site_links(
                        page.html, page.url, skip=visited):
                    visited.add(link)
                    candidates.append(link)
            candidates = candidates[:remaining]
            if not candidates:
                break
            attempted_pages += len(candidates)
            await asyncio.sleep(0.5)
            fetched, deadline_reached = await gather_before_deadline(
                (fetch_one(u) for u in candidates),
                deadline,
            )
            current_level = []
            for page in fetched:
                if page is None:
                    continue
                normalized = normalize_url(page.url)
                if normalized in stored_pages:
                    continue
                stored_pages.add(normalized)
                visited.add(normalized)
                current_level.append(page)
            result.pages.extend(current_level)
            if deadline_reached:
                break
    return result


async def _crawl_render(start_url: str, depth: int,
                        max_pages: int = RENDER_MAX_PAGES,
                        deadline: float | None = None,
                        budget: Any | None = None,
                        since: str | None = None) -> CrawlResult:
    """BFS over rendered pages: links come from the live DOM (including
    pagination harvest), so JS-injected list items are discoverable.
    since 为 YYYY-MM-DD 格式的时间下限，发布日早于 since 的页面将被跳过。"""
    import renderer

    result = CrawlResult()
    start_norm = normalize_url(start_url)
    effective_authority: tuple[str, int] | None = None

    def usable(link: str, base_url: str) -> str | None:
        absolute = urljoin(base_url, link)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            return None
        if canonical_authority(absolute) != effective_authority:
            return None
        normalized = normalize_url(absolute)
        if normalized == start_norm or is_binary_url(normalized):
            return None
        return normalized

    try:
        start_render_kwargs = {
            "navigation_allowed": lambda target: same_site_boundary(
                start_url,
                target,
            ),
        }
        if budget is not None:
            start_render_kwargs["reserve_request"] = budget.reserve_html
        start_loaded, deadline_reached = await await_before_deadline(
            renderer.render_page_result(
                start_url,
                **start_render_kwargs,
            ),
            deadline,
        )
    except renderer.RenderError as exc:
        raise ValueError(str(exc)) from exc
    if deadline_reached:
        result.failed.append({
            "url": start_url,
            "reason": "搜索截止时间已到",
        })
        return result
    if not same_site_boundary(start_url, start_loaded.final_url):
        raise ValueError("重定向到站外地址")
    effective_authority = canonical_authority(start_loaded.final_url)
    if effective_authority is None:
        raise ValueError("重定向后的地址无效")
    start_page = CrawledPage(
        url=start_loaded.final_url,
        html=start_loaded.html,
    )
    start_links = start_loaded.links
    start_norm = normalize_url(start_page.url)
    result.pages.append(start_page)
    visited = {normalize_url(start_url), start_norm}
    attempted_pages = 1
    current_level: list[tuple[str, list[str]]] = [
        (start_page.url, start_links)
    ]

    async def render_one(url: str):
        try:
            render_kwargs = {
                "navigation_allowed": lambda target: (
                    canonical_authority(target) == effective_authority
                ),
            }
            if budget is not None:
                render_kwargs["reserve_request"] = budget.reserve_html
            rendered = await renderer.render_page_result(
                url,
                **render_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - collected, not raised
            result.failed.append(
                {"url": url, "reason": describe_error(exc)})
            return None
        if canonical_authority(rendered.final_url) != effective_authority:
            result.failed.append({
                "url": url,
                "reason": "重定向到站外地址",
            })
            return None
        if since:
            page_date = extract_published_date(
                rendered.html, rendered.final_url)
            if page_date and page_date < since:
                return None
        return (
            CrawledPage(url=rendered.final_url, html=rendered.html),
            rendered.links,
        )

    async def fetch_static_articles(urls: list[str]) -> list[CrawledPage]:
        semaphore = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=PAGE_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            static_context = _CrawlHttpContext(
                client,
                lambda target: (
                    canonical_authority(target) == effective_authority
                ),
                None if budget is None else budget.reserve_html,
            )

            async def fetch_one(url: str) -> CrawledPage | None:
                async with semaphore:
                    try:
                        value = await _fetch_crawl_html(
                            static_context,
                            url,
                            2,
                            1.0,
                        )
                    except UnsafeRedirect:
                        result.failed.append({
                            "url": url,
                            "reason": "重定向到站外地址",
                        })
                        return None
                    except PageBudgetExhausted:
                        result.failed.append({
                            "url": url,
                            "reason": "页面预算已用尽",
                        })
                        return None
                    except Exception as exc:  # noqa: BLE001 - collected, not raised
                        result.failed.append(
                            {"url": url, "reason": describe_error(exc)})
                        return None
                    fetched = _as_fetched_html(value, url)
                    if since:
                        page_date = extract_published_date(
                            fetched.html, fetched.final_url)
                        if page_date and page_date < since:
                            return None
                    return CrawledPage(
                        url=fetched.final_url,
                        html=fetched.html,
                    )

            fetched, _deadline_reached = await gather_before_deadline(
                (fetch_one(url) for url in urls),
                deadline,
            )
        return [page for page in fetched if page is not None]

    remaining = max_pages - attempted_pages
    discovery_hubs: list[str] = []
    for link in prioritize_render_links(start_links):
        if len(discovery_hubs) >= min(RENDER_DISCOVERY_HUBS, remaining):
            break
        normalized = usable(link, start_page.url)
        if (normalized is None or normalized in visited
                or not is_render_discovery_hub(normalized)):
            continue
        visited.add(normalized)
        discovery_hubs.append(normalized)
    attempted_pages += len(discovery_hubs)

    rendered_hubs, hubs_deadline_reached = await gather_before_deadline(
        (render_one(url) for url in discovery_hubs),
        deadline,
    )
    successful_hubs = [item for item in rendered_hubs if item is not None]
    result.pages.extend(page for page, _links in successful_hubs)
    if hubs_deadline_reached:
        return result

    discovery_articles: list[str] = []
    reserved_discovery_articles: set[str] = set()
    remaining = max_pages - attempted_pages
    for hub_page, hub_links in successful_hubs:
        per_hub = 0
        for link in prioritize_render_links(hub_links):
            if (per_hub >= RENDER_DISCOVERY_ARTICLES_PER_HUB
                    or len(discovery_articles) >= remaining):
                break
            normalized = usable(link, hub_page.url)
            if (normalized is None or normalized in visited
                    or normalized in reserved_discovery_articles
                    or not is_render_article_link(normalized)):
                continue
            reserved_discovery_articles.add(normalized)
            discovery_articles.append(normalized)
            per_hub += 1
    attempted_pages += len(discovery_articles)
    static_articles = await fetch_static_articles(discovery_articles)
    visited.update(normalize_url(page.url) for page in static_articles)
    result.pages.extend(static_articles)
    if deadline is not None and time.monotonic() >= deadline:
        return result

    for _level in range(depth):
        remaining = max_pages - attempted_pages
        if remaining <= 0:
            break
        candidates: list[str] = []
        for page_url, links in current_level:
            per_page = 0
            for link in links:
                if per_page >= RENDER_SUBPAGE_LINKS:
                    break
                normalized = usable(link, page_url)
                if normalized is None or normalized in visited:
                    continue
                visited.add(normalized)
                candidates.append(normalized)
                per_page += 1
        candidates = candidates[:remaining]
        attempted_pages += len(candidates)
        fetched, deadline_reached = await gather_before_deadline(
            (render_one(u) for u in candidates),
            deadline,
        )
        ok = [item for item in fetched if item is not None]
        current_level = [(p.url, links) for p, links in ok]
        if _level == 0:
            current_level.extend(
                (page.url, links) for page, links in successful_hubs)
        result.pages.extend(p for p, _links in ok)
        if deadline_reached or not current_level:
            break
    return result


ARCHIVE_MAX_PAGES = 2000
ARCHIVE_HUB_LIMIT = 12
ARCHIVE_BUDGET_SECONDS = 1200


async def crawl_archive(start_url: str,
                        deadline: float | None = None,
                        budget: Any | None = None,
                        since: str | None = None) -> CrawlResult:
    """归档深扫：渲染起始页与栏目列表（含全量翻页收割），把发现的
    全部文章链接的正文静态抓回。

    常规搜索依赖站内搜索接口（只索引标题）和有限的栏目翻页，标题里
    不含关键词的旧文、正文独有的词都覆盖不到；归档深扫以时间为代价
    （可能 10~20 分钟）换取正文全覆盖。起始页渲染失败会抛出，其余
    失败收集进 CrawlResult.failed。
    since 为 YYYY-MM-DD 格式的时间下限，发布日早于 since 的文章将被跳过。
    """
    import renderer

    result = CrawlResult()

    render_kwargs: dict = {
        "navigation_allowed": lambda target: same_site_boundary(
            start_url, target),
    }
    if budget is not None:
        render_kwargs["reserve_request"] = budget.reserve_html
    try:
        start_loaded, deadline_reached = await await_before_deadline(
            renderer.render_page_result(start_url, **render_kwargs),
            deadline,
        )
    except renderer.RenderError as exc:
        raise ValueError(str(exc)) from exc
    if deadline_reached:
        result.failed.append({"url": start_url, "reason": "搜索截止时间已到"})
        return result
    if not same_site_boundary(start_url, start_loaded.final_url):
        raise ValueError("重定向到站外地址")

    start_page = CrawledPage(url=start_loaded.final_url,
                             html=start_loaded.html)
    result.pages.append(start_page)
    visited = {normalize_url(start_url), normalize_url(start_page.url)}

    def usable(link: str, base_url: str) -> str | None:
        absolute = urljoin(base_url, link)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            return None
        if not same_site_boundary(start_page.url, absolute):
            return None
        normalized = normalize_url(absolute)
        if normalized in visited or is_binary_url(normalized):
            return None
        return normalized

    # 栏目列表（hub）：渲染时各自完成全量翻页收割
    hubs: list[str] = []
    for link in prioritize_render_links(start_loaded.links):
        if len(hubs) >= ARCHIVE_HUB_LIMIT:
            break
        normalized = usable(link, start_page.url)
        if normalized is None or not is_render_discovery_hub(normalized):
            continue
        visited.add(normalized)
        hubs.append(normalized)

    async def render_hub(url: str):
        try:
            kwargs = {
                "navigation_allowed": lambda target: same_site_boundary(
                    start_page.url, target),
            }
            if budget is not None:
                kwargs["reserve_request"] = budget.reserve_html
            return await renderer.render_page_result(url, **kwargs)
        except Exception as exc:  # noqa: BLE001 - collected, not raised
            result.failed.append({"url": url, "reason": describe_error(exc)})
            return None

    rendered_hubs, hubs_deadline_reached = await gather_before_deadline(
        (render_hub(u) for u in hubs), deadline)
    hub_pages = [r for r in rendered_hubs
                 if r is not None
                 and same_site_boundary(start_page.url, r.final_url)]
    result.pages.extend(
        CrawledPage(url=r.final_url, html=r.html) for r in hub_pages)
    if hubs_deadline_reached:
        return result

    # 收集全部文章链接（起始页本身可能就是列表页）
    articles: list[str] = []
    for link in start_loaded.links:
        normalized = usable(link, start_page.url)
        if normalized and is_render_article_link(normalized):
            visited.add(normalized)
            articles.append(normalized)
    for hub in hub_pages:
        for link in hub.links:
            normalized = usable(link, hub.final_url)
            if normalized and is_render_article_link(normalized):
                visited.add(normalized)
                articles.append(normalized)
    articles = articles[:ARCHIVE_MAX_PAGES]

    # 静态抓取全部文章正文
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        static_context = _CrawlHttpContext(
            client,
            lambda target: same_site_boundary(start_page.url, target),
            None if budget is None else budget.reserve_html,
        )

        async def fetch_one(url: str) -> CrawledPage | None:
            async with semaphore:
                try:
                    value = await _fetch_crawl_html(
                        static_context, url, 2, 1.0)
                except UnsafeRedirect:
                    result.failed.append(
                        {"url": url, "reason": "重定向到站外地址"})
                    return None
                except PageBudgetExhausted:
                    result.failed.append(
                        {"url": url, "reason": "页面预算已用尽"})
                    return None
                except Exception as exc:  # noqa: BLE001 - collected
                    result.failed.append(
                        {"url": url, "reason": describe_error(exc)})
                    return None
                fetched = _as_fetched_html(value, url)
                if since:
                    page_date = extract_published_date(
                        fetched.html, fetched.final_url)
                    if page_date and page_date < since:
                        return None
                return CrawledPage(url=fetched.final_url, html=fetched.html)

        fetched, _ = await gather_before_deadline(
            (fetch_one(u) for u in articles), deadline)
        result.pages.extend(p for p in fetched if p is not None)
    return result
