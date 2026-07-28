"""站点自带搜索接口探测与结果页抓取。

部分政府站 CMS（如云南政府站群）提供可直接 GET 的站内搜索接口：
- /searchClassCount.aspx?tags=词          → JSON {code:0, data:[{Count}]}
- /searchN.aspx?page=N&type=&tags=词     → HTML 结果列表（分页文本 "1 / N"）

栏目列表翻页太深时（旧文章沉在几十页之后），BFS 爬不到；站内搜索接口
按标题索引全站文章，把结果链接取回并抓全文补充进匹配，即可找回旧内容。
能力与官网搜索一致：只覆盖标题命中的文章，正文里独有的词仍依赖翻页。
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from crawler import (CONCURRENCY, PAGE_TIMEOUT, USER_AGENT, CrawledPage,
                     fetch_html_retry, is_binary_url, normalize_url)

COUNT_PATH = "/searchClassCount.aspx"
LIST_PATH = "/searchN.aspx"
MAX_RESULT_PAGES = 10
MAX_KEYWORDS = 6
MAX_EXTRA_PAGES = 40

_probe_cache: dict[str, bool] = {}
_PAGER_RE = re.compile(r"\d+\s*/\s*(\d+)")


async def probe(client: httpx.AsyncClient, origin: str) -> bool:
    """探测站点是否支持该 CMS 的站内搜索接口（按 origin 缓存结果）。"""
    if origin in _probe_cache:
        return _probe_cache[origin]
    ok = False
    try:
        resp = await client.get(origin + COUNT_PATH, params={"tags": "检测"})
        data = resp.json()
        ok = bool(resp.status_code == 200 and isinstance(data, dict)
                  and data.get("code") == 0
                  and isinstance(data.get("data"), list))
    except Exception:  # noqa: BLE001 - 任何失败都视为不支持
        ok = False
    _probe_cache[origin] = ok
    return ok


def parse_result_links(html: str, origin: str) -> list[str]:
    """从搜索结果页 HTML 提取同站文章链接（去重、去分页器/外链/二进制）。"""
    origin_host = urlsplit(origin).netloc.lower()
    root = normalize_url(origin + "/")
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        raw = a["href"].strip()
        if not raw or raw == "#" or raw.lower().startswith("javascript:"):
            continue
        absolute = urljoin(origin + "/", raw)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower() != origin_host:
            continue
        if parts.path in (LIST_PATH, COUNT_PATH) or \
                parts.path.lower().endswith("search.html"):
            continue
        normalized = normalize_url(absolute)
        if normalized == root or is_binary_url(normalized) \
                or normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
    return links


def parse_page_count(html: str) -> int:
    """从分页文本 "1 / N" 解析总页数，找不到则为 1。"""
    m = _PAGER_RE.search(html)
    if not m:
        return 1
    try:
        return max(1, int(m.group(1)))
    except ValueError:
        return 1


async def _result_links(client: httpx.AsyncClient, origin: str,
                        keyword: str) -> list[str]:
    links: list[str] = []
    total = 1
    page = 1
    while page <= min(total, MAX_RESULT_PAGES):
        resp = await client.get(
            origin + LIST_PATH,
            params={"page": page, "type": "", "tags": keyword})
        resp.raise_for_status()
        html = resp.text
        if page == 1:
            total = parse_page_count(html)
        links.extend(parse_result_links(html, origin))
        page += 1
    seen: set[str] = set()
    unique: list[str] = []
    for u in links:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


async def collect_pages(start_url: str, keywords: list[str],
                        skip: set[str] | frozenset[str] = frozenset()
                        ) -> tuple[list[CrawledPage], dict]:
    """用站内搜索接口按关键词收集结果文章并抓取全文。

    返回 (额外页面列表, 信息字典)。站点不支持或任何环节失败都返回
    ([], info)，绝不抛出——这只是一个补充通道。
    """
    info = {"available": False, "linksFound": 0, "pagesFetched": 0}
    parts = urlsplit(start_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        async with httpx.AsyncClient(
            timeout=PAGE_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            if not await probe(client, origin):
                return [], info
            info["available"] = True
            seen = {normalize_url(u) for u in skip}
            candidates: list[str] = []
            for kw in keywords[:MAX_KEYWORDS]:
                try:
                    for u in await _result_links(client, origin, kw):
                        if u not in seen:
                            seen.add(u)
                            candidates.append(u)
                except Exception:  # noqa: BLE001 - 单个词失败不影响其他词
                    continue
            candidates = candidates[:MAX_EXTRA_PAGES]
            info["linksFound"] = len(candidates)
            if not candidates:
                return [], info
            sem = asyncio.Semaphore(CONCURRENCY)

            async def fetch_one(url: str) -> CrawledPage | None:
                async with sem:
                    try:
                        html = await fetch_html_retry(
                            client, url, attempts=2, base_delay=1.0)
                        return CrawledPage(url=url, html=html)
                    except Exception:  # noqa: BLE001 - 单页失败忽略
                        return None

            fetched = await asyncio.gather(
                *(fetch_one(u) for u in candidates))
            pages = [p for p in fetched if p is not None]
            info["pagesFetched"] = len(pages)
            return pages, info
    except Exception:  # noqa: BLE001 - 补充通道失败不阻断主流程
        return [], {"available": False, "linksFound": 0, "pagesFetched": 0}
