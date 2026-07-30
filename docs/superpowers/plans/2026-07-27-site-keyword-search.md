# 站内关键词搜索工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本地网页工具：输入起始网址和关键词，抓取该页面及同站一层子页面做关键词搜索（正文 + 图片/属性等各类标记元素），展示命中位置与跳转链接，全部未命中时明确提示关键词不存在。

**Architecture:** 单个 FastAPI 进程（`server.py`）托管前端静态页（`static/index.html`）并提供 `POST /api/search`；`crawler.py` 负责抓取起始页、提取同站链接、asyncio + httpx 并发抓取子页面；`matcher.py` 负责 HTML 解析与关键词匹配。无数据库，即搜即弃。

**Tech Stack:** Python 3.12（managed runtime）、FastAPI 0.140、uvicorn 0.51、httpx 0.28、BeautifulSoup4 4.14、pytest 9.1、Node 24（仅用于 `npm run dev` 转发启动）。

**Spec:** `docs/superpowers/specs/2026-07-27-site-keyword-search-design.md`（已与用户确认）

## Global Constraints

- 工作区根目录：`D:\WebProjects\web_keyword_catcher`，所有文件必须创建在该目录内。
- 默认监听 `127.0.0.1:7100`；`npm run dev` 必须能把 CLI 的 `--host` / `--port` 参数转发给 Python 服务。
- `MAX_SUBPAGES = 30`（同站子页面上限）、`CONCURRENCY = 8`（并发度）、`PAGE_TIMEOUT = 10.0`（单页超时秒）、`SEARCH_BUDGET_SECONDS = 120`（整体超时秒）——数值与 spec 一致。
- 匹配规则：大小写不敏感、子串匹配、多关键词空格分隔任一命中即算；上下文片段为关键词前后各约 60 字符。
- 响应 JSON 相比 spec §3 增加一个字段 `crawledPages: string[]`（全部成功抓取页面的 URL 列表），用于支撑 spec §6"未命中时列出已抓取页面清单"的要求。
- 界面文案使用中文；代码注释、提交信息使用英文或中文均可，遵循本仓库惯例（目前仅 docs 提交）。
- TDD：每个后端任务先写失败测试再实现；每个任务结束立即 commit。

## File Structure

```
server.py            FastAPI 应用：GET / 托管前端 + POST /api/search
crawler.py           抓取起始页、提取同站链接、并发抓取子页面
matcher.py           页面解析与关键词匹配（全部搜索位置）+ 标题提取
static/index.html    前端单页面：输入、进度动画、分组结果、未命中提示
scripts/dev.mjs      npm run dev 入口：解析 --host/--port 并 spawn python server.py
package.json         dev 脚本定义
tests/conftest.py    本地 HTTP 夹具服务器 fixture
tests/fixtures/site/ 夹具 HTML 页面（index/sub1/sub2）
tests/test_matcher.py
tests/test_crawler.py
tests/test_server.py
```

---

### Task 1: matcher.py — 关键词匹配器

**Files:**
- Create: `matcher.py`
- Test: `tests/test_matcher.py`

**Interfaces:**
- Produces（后续任务依赖）:
  - `Hit` dataclass，字段：`kind: str`、`snippet: str`、`keyword: str`、`href: str`、`linkHref: str | None = None`
  - `match_page(html: str, page_url: str, keywords: list[str]) -> list[Hit]`
  - `extract_title(html: str) -> str`
  - `make_snippet(text: str, keyword: str) -> str | None`
  - 常量 `SNIPPET_RADIUS = 60`
- `kind` 取值：`text | img-alt | img-src | title-attr | aria-label | link-text | link-href | meta | form`
- `href` 规则：`text` 命中为 `page_url + "#:~:text=" + quote(keyword)`；其余 kind 为 `page_url`。

- [ ] **Step 1: Write the failing test**

创建 `tests/test_matcher.py`：

```python
"""matcher.py 的单元测试：覆盖全部 kind、大小写、片段、去重。"""
from matcher import match_page, extract_title, make_snippet, SNIPPET_RADIUS

PAGE = "http://test.local/page"

HTML = """
<html><head>
<title>Home Page</title>
<meta name="description" content="Delta site overview">
<meta property="og:title" content="Delta OG title">
<meta name="viewport" content="width=device-width">
<script>var keywordInsideScript = "Alpha";</script>
<style>.alpha-class { color: red; }</style>
</head><body>
<p>Visible Alpha text in body.</p>
<a href="/sub1">Epsilon page</a>
<a href="https://other.example.com/zeta-path">outer</a>
<img src="/img/theta-chart.png" alt="Beta diagram" title="Iota info">
<nav aria-label="Gamma navigation">nav</nav>
<input placeholder="Zeta search box" value="Kappa value">
<!-- Alpha in comment -->
</body></html>
"""


def kinds(hits):
    return sorted({h.kind for h in hits})


def test_text_hit():
    hits = match_page(HTML, PAGE, ["alpha"])
    text_hits = [h for h in hits if h.kind == "text"]
    assert len(text_hits) == 1  # script/style/comment/a 内文本不算 text
    assert "Visible Alpha text" in text_hits[0].snippet
    assert text_hits[0].href == PAGE + "#:~:text=alpha"
    assert text_hits[0].keyword == "alpha"


def test_all_kinds():
    hits = match_page(HTML, PAGE,
                      ["beta", "theta", "iota", "gamma", "epsilon",
                       "zeta", "kappa", "delta"])
    assert kinds(hits) == ["aria-label", "form", "img-alt", "img-src",
                           "link-href", "link-text", "meta", "text", "title-attr"] or \
           set(kinds(hits)) >= {"img-alt", "img-src", "title-attr", "aria-label",
                                "link-text", "link-href", "meta", "form"}


def test_img_alt_and_src_and_title():
    hits = match_page(HTML, PAGE, ["beta", "theta", "iota"])
    by_kind = {h.kind: h for h in hits}
    assert by_kind["img-alt"].snippet == "Beta diagram"
    assert by_kind["img-src"].snippet == "theta-chart.png"
    assert by_kind["title-attr"].snippet == "Iota info"
    assert all(h.href == PAGE for h in hits)


def test_link_kinds():
    hits = match_page(HTML, PAGE, ["epsilon", "zeta"])
    lt = [h for h in hits if h.kind == "link-text"][0]
    lh = [h for h in hits if h.kind == "link-href"][0]
    assert lt.snippet == "Epsilon page"
    assert lt.linkHref == "/sub1"
    assert lh.linkHref == "https://other.example.com/zeta-path"
    assert "zeta-path" in lh.snippet


def test_meta_hit_excludes_viewport():
    hits = match_page(HTML, PAGE, ["delta", "device-width"])
    meta_hits = [h for h in hits if h.kind == "meta"]
    assert len(meta_hits) == 2  # description + og:title，不含 viewport
    assert {h.snippet for h in meta_hits} == {"Delta site overview", "Delta OG title"}


def test_form_hits():
    hits = match_page(HTML, PAGE, ["zeta", "kappa"])
    form_hits = [h for h in hits if h.kind == "form"]
    assert {h.snippet for h in form_hits} == {"Zeta search box", "Kappa value"}


def test_case_insensitive_and_no_match():
    assert match_page(HTML, PAGE, ["ALPHA"]) != []
    assert match_page(HTML, PAGE, ["not-present-anywhere"]) == []


def test_snippet_radius():
    text = "x" * 200 + "Keyword" + "y" * 200
    snippet = make_snippet(text, "keyword")
    assert snippet.startswith("…") and snippet.endswith("…")
    assert snippet.index("Keyword") == SNIPPET_RADIUS + 1  # 省略号占 1 字符
    assert make_snippet("nothing here", "keyword") is None


def test_dedupe_same_kind_and_snippet():
    html = "<p>dup Alpha</p><p>dup Alpha</p>"
    hits = match_page(html, PAGE, ["alpha"])
    assert len(hits) == 1


def test_extract_title():
    assert extract_title(HTML) == "Home Page"
    assert extract_title("<html><body>no title</body></html>") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/test_matcher.py -v`
Expected: FAIL，报 `ModuleNotFoundError: No module named 'matcher'`

- [ ] **Step 3: Write minimal implementation**

创建 `matcher.py`：

```python
"""Keyword matcher: search keywords across visible text and element attributes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup, Comment

SNIPPET_RADIUS = 60

META_NAME_KEYS = {"keywords", "description"}
SKIP_TEXT_PARENTS = {"script", "style", "noscript", "a", "title"}


@dataclass
class Hit:
    kind: str
    snippet: str
    keyword: str
    href: str
    linkHref: str | None = None


def make_snippet(text: str, keyword: str) -> str | None:
    """Return ~60 chars of context around the first case-insensitive match."""
    norm = re.sub(r"\s+", " ", text)
    idx = norm.lower().find(keyword.lower())
    if idx < 0:
        return None
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(norm), idx + len(keyword) + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(norm) else ""
    return prefix + norm[start:end] + suffix


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def match_page(html: str, page_url: str, keywords: list[str]) -> list[Hit]:
    soup = BeautifulSoup(html, "html.parser")
    keywords = [k for k in keywords if k.strip()]
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, raw_text: str, keyword: str,
            link_href: str | None = None) -> None:
        snippet = make_snippet(raw_text, keyword)
        if snippet is None:
            return
        key = (kind, snippet)
        if key in seen:
            return
        seen.add(key)
        if kind == "text":
            href = f"{page_url}#:~:text={quote(keyword)}"
        else:
            href = page_url
        hits.append(Hit(kind=kind, snippet=snippet, keyword=keyword,
                        href=href, linkHref=link_href))

    # text: visible body text (skip script/style/noscript/a/title nodes)
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            continue
        if node.parent and node.parent.name in SKIP_TEXT_PARENTS:
            continue
        if not str(node).strip():
            continue
        for kw in keywords:
            add("text", str(node), kw)

    # img-alt / img-src
    for img in soup.find_all("img"):
        alt = img.get("alt")
        if alt:
            for kw in keywords:
                add("img-alt", alt, kw)
        src = img.get("src")
        if src:
            filename = urlsplit(src).path.rsplit("/", 1)[-1]
            if filename:
                for kw in keywords:
                    add("img-src", filename, kw)

    # title-attr / aria-label
    for el in soup.find_all(attrs={"title": True}):
        for kw in keywords:
            add("title-attr", el["title"], kw)
    for el in soup.find_all(attrs={"aria-label": True}):
        for kw in keywords:
            add("aria-label", el["aria-label"], kw)

    # link-text / link-href
    for a in soup.find_all("a", href=True):
        for kw in keywords:
            add("link-text", a.get_text(), kw, link_href=a["href"])
            add("link-href", a["href"], kw, link_href=a["href"])

    # meta
    for meta in soup.find_all("meta"):
        content = meta.get("content")
        if not content:
            continue
        name = (meta.get("name") or "").lower()
        prop = (meta.get("property") or "").lower()
        if name in META_NAME_KEYS or prop.startswith("og:") \
                or name.startswith("twitter:"):
            for kw in keywords:
                add("meta", content, kw)

    # form
    for el in soup.find_all(["input", "textarea"]):
        for attr in ("placeholder", "value"):
            val = el.get(attr)
            if val:
                for kw in keywords:
                    add("form", val, kw)

    return hits
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/test_matcher.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
cd /d D:\WebProjects\web_keyword_catcher
git add matcher.py tests/test_matcher.py
git commit -m "feat: keyword matcher covering text and attribute positions"
```

---

### Task 2: crawler.py — 链接提取与并发抓取

**Files:**
- Create: `crawler.py`
- Create: `tests/conftest.py`、`tests/fixtures/site/index.html`、`tests/fixtures/site/sub1.html`、`tests/fixtures/site/sub2.html`
- Test: `tests/test_crawler.py`

**Interfaces:**
- Consumes: 无（不依赖 matcher）
- Produces（server.py 依赖）:
  - `CrawledPage` dataclass：`url: str`、`html: str`
  - `CrawlResult` dataclass：`pages: list[CrawledPage]`、`failed: list[dict]`（dict 形如 `{"url": str, "reason": str}`）
  - `async def crawl(start_url: str) -> CrawlResult` —— 起始页抓取失败时抛异常（httpx.HTTPStatusError / httpx.TimeoutException / ValueError 等）；子页面失败只记入 `failed`
  - `extract_same_site_links(html: str, base_url: str, limit: int = MAX_SUBPAGES) -> list[str]`
  - `normalize_url(url: str) -> str`、`is_binary_url(url: str) -> bool`
  - 常量 `MAX_SUBPAGES = 30`、`CONCURRENCY = 8`、`PAGE_TIMEOUT = 10.0`

- [ ] **Step 1: Write the failing test fixtures and tests**

创建 `tests/fixtures/site/index.html`：

```html
<html><head><title>Fixture Home</title>
<meta name="description" content="Delta site overview">
</head><body>
<p>Visible Alpha text on home.</p>
<a href="/sub1.html">Epsilon page one</a>
<a href="/sub1.html#frag">dup with anchor</a>
<a href="/sub1.html/">dup trailing slash</a>
<a href="/sub2.html">Sub two</a>
<a href="/missing.html">Missing page</a>
<a href="https://external.example.com/x">External</a>
<a href="/img/logo.png">Logo file</a>
<a href="mailto:a@b.c">Mail</a>
<img src="/img/theta-chart.png" alt="Beta diagram">
</body></html>
```

创建 `tests/fixtures/site/sub1.html`：

```html
<html><head><title>Sub One</title></head><body>
<p>Alpha again on sub one.</p>
<nav aria-label="Gamma navigation">nav</nav>
</body></html>
```

创建 `tests/fixtures/site/sub2.html`：

```html
<html><head><title>Sub Two</title></head><body>
<p>Plain content without target words.</p>
</body></html>
```

创建 `tests/conftest.py`：

```python
"""Threaded HTTP fixture server serving tests/fixtures/site."""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

FIXTURE_SITE = Path(__file__).parent / "fixtures" / "site"


@pytest.fixture(scope="session")
def site_server():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(FIXTURE_SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
```

创建 `tests/test_crawler.py`：

```python
"""crawler.py 的单元测试：链接提取、规范化、并发抓取与失败记录。"""
from pathlib import Path

import pytest

from crawler import (CONCURRENCY, MAX_SUBPAGES, crawl,
                     extract_same_site_links, is_binary_url, normalize_url)

FIXTURE_SITE = Path(__file__).parent / "fixtures" / "site"


def test_normalize_url():
    assert normalize_url("http://h/a.html#frag") == "http://h/a.html"
    assert normalize_url("http://h/a.html/") == "http://h/a.html"
    assert normalize_url("http://h/") == "http://h/"
    assert normalize_url("HTTP://H/a.html") == "http://h/a.html"


def test_is_binary_url():
    assert is_binary_url("http://h/img/logo.png")
    assert is_binary_url("http://h/files/doc.PDF")
    assert not is_binary_url("http://h/page.html")
    assert not is_binary_url("http://h/page")


def test_extract_same_site_links(site_server):
    html = (FIXTURE_SITE / "index.html").read_text(encoding="utf-8")
    base = f"{site_server}/index.html"
    links = extract_same_site_links(html, base)
    assert links == [
        f"{site_server}/sub1.html",
        f"{site_server}/sub2.html",
        f"{site_server}/missing.html",
    ]  # 外部链接、二进制、mailto、起始页自身、重复项均被排除


def test_extract_respects_limit(site_server):
    html = "".join(f'<a href="/p{i}.html">p{i}</a>' for i in range(50))
    links = extract_same_site_links(html, f"{site_server}/index.html")
    assert len(links) == MAX_SUBPAGES


@pytest.mark.asyncio
async def test_crawl_collects_pages_and_failures(site_server):
    import httpx
    try:
        import pytest_asyncio  # noqa: F401
    except ImportError:
        pytest.skip("pytest-asyncio not installed; run via asyncio.run test")

    result = await crawl(f"{site_server}/index.html")
    urls = {p.url for p in result.pages}
    assert urls == {f"{site_server}/index.html",
                    f"{site_server}/sub1.html",
                    f"{site_server}/sub2.html"}
    assert result.failed == [
        {"url": f"{site_server}/missing.html", "reason": "HTTP 404"}]


def test_crawl_via_asyncio_run(site_server):
    import asyncio
    result = asyncio.run(crawl(f"{site_server}/index.html"))
    assert len(result.pages) == 3
    assert result.failed[0]["reason"] == "HTTP 404"


def test_crawl_start_page_failure_raises(site_server):
    import asyncio
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(crawl(f"{site_server}/missing.html"))


def test_constants():
    assert MAX_SUBPAGES == 30
    assert CONCURRENCY == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/test_crawler.py -v`
Expected: FAIL，报 `ModuleNotFoundError: No module named 'crawler'`

- [ ] **Step 3: Write minimal implementation**

创建 `crawler.py`：

```python
"""Crawler: fetch start page, extract same-site links, fetch subpages concurrently."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

MAX_SUBPAGES = 30
CONCURRENCY = 8
PAGE_TIMEOUT = 10.0
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


@dataclass
class CrawlResult:
    pages: list[CrawledPage] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{scheme}://{netloc}{path}" + (
        f"?{parts.query}" if parts.query else "")


def is_binary_url(url: str) -> bool:
    filename = urlsplit(url).path.rsplit("/", 1)[-1]
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[-1].lower() in BINARY_EXTENSIONS


def extract_same_site_links(html: str, base_url: str,
                            limit: int = MAX_SUBPAGES) -> list[str]:
    base_host = urlsplit(base_url).netloc.lower()
    base_norm = normalize_url(base_url)
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"])
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower() != base_host:
            continue
        normalized = normalize_url(absolute)
        if normalized == base_norm or is_binary_url(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= limit:
            break
    return links


def describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "访问超时"
    if isinstance(exc, httpx.ConnectError):
        return "连接失败"
    if isinstance(exc, ValueError):
        return str(exc)
    return type(exc).__name__


async def fetch_html(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise ValueError(f"非 HTML 内容 ({content_type or '未知类型'})")
    return response.text


async def crawl(start_url: str) -> CrawlResult:
    """Fetch start page and its same-site subpages.

    Raises if the start page itself fails; subpage failures are collected
    into CrawlResult.failed.
    """
    result = CrawlResult()
    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        start_html = await fetch_html(client, start_url)
        result.pages.append(CrawledPage(url=start_url, html=start_html))
        links = extract_same_site_links(start_html, start_url)
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def fetch_one(url: str) -> None:
            async with semaphore:
                try:
                    html = await fetch_html(client, url)
                except Exception as exc:  # noqa: BLE001 - collected, not raised
                    result.failed.append(
                        {"url": url, "reason": describe_error(exc)})
                    return
                result.pages.append(CrawledPage(url=url, html=html))

        await asyncio.gather(*(fetch_one(u) for u in links))
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/test_crawler.py -v`
Expected: 8 passed（`test_crawl_collects_pages_and_failures` 若因缺 pytest-asyncio 被 skip 也算通过，以 `test_crawl_via_asyncio_run` 覆盖同一逻辑）

- [ ] **Step 5: Commit**

```bash
cd /d D:\WebProjects\web_keyword_catcher
git add crawler.py tests/conftest.py tests/fixtures tests/test_crawler.py
git commit -m "feat: same-site link extraction and concurrent crawler"
```

---

### Task 3: server.py — FastAPI 应用与搜索接口

**Files:**
- Create: `server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `crawl(start_url) -> CrawlResult`（Task 2）；`match_page(html, page_url, keywords) -> list[Hit]`、`extract_title(html) -> str`（Task 1）
- Produces:
  - `app`（FastAPI 实例）；`GET /` 返回 `static/index.html`；`POST /api/search`
  - 请求体：`{"startUrl": str, "keywords": [str] 或空格分隔 str}`
  - 响应体（在 spec §3 基础上增加 `crawledPages`）：
    `{"startUrl", "keywords": [str], "pagesCrawled": int, "crawledPages": [str], "pagesFailed": [{"url","reason"}], "totalHits": int, "results": [{"pageUrl","pageTitle","hits":[{"kind","snippet","keyword","href","linkHref"}]}]}`
  - 错误：URL/关键词非法 → 422；起始页不可达/非 HTML → 502；整体超 120 秒 → 504

- [ ] **Step 1: Write the failing test**

创建 `tests/test_server.py`：

```python
"""server.py 的接口测试：端到端搜索、未命中、参数校验、起始页失败。"""
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


def search(site_server, keywords, path="/index.html"):
    return client.post("/api/search", json={
        "startUrl": f"{site_server}{path}",
        "keywords": keywords,
    })


def test_search_hit_structure(site_server):
    resp = search(site_server, ["alpha", "beta"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagesCrawled"] == 3
    assert len(data["crawledPages"]) == 3
    assert data["pagesFailed"] == [
        {"url": data["crawledPages"][0].rsplit("/", 1)[0] + "/missing.html",
         "reason": "HTTP 404"}]
    assert data["totalHits"] > 0
    page = next(r for r in data["results"] if r["pageUrl"].endswith("/index.html"))
    assert page["pageTitle"] == "Fixture Home"
    hit_kinds = {h["kind"] for h in page["hits"]}
    assert "text" in hit_kinds and "img-alt" in hit_kinds
    for hit in page["hits"]:
        assert set(hit) == {"kind", "snippet", "keyword", "href", "linkHref"}
    text_hit = next(h for h in page["hits"] if h["kind"] == "text")
    assert "#:~:text=" in text_hit["href"]


def test_search_no_hit(site_server):
    resp = search(site_server, ["absent-word"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalHits"] == 0
    assert data["results"] == []
    assert len(data["crawledPages"]) == 3


def test_search_keywords_as_string(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": "alpha beta",
    })
    assert resp.status_code == 200
    assert resp.json()["keywords"] == ["alpha", "beta"]


def test_invalid_url_rejected(site_server):
    resp = client.post("/api/search", json={
        "startUrl": "ftp://bad", "keywords": ["x"]})
    assert resp.status_code == 422


def test_empty_keywords_rejected(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html", "keywords": ["  "]})
    assert resp.status_code == 422


def test_start_page_failure_returns_502(site_server):
    resp = search(site_server, ["x"], path="/missing.html")
    assert resp.status_code == 502
    assert "404" in resp.json()["detail"]


def test_index_page_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "站内关键词搜索" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/test_server.py -v`
Expected: FAIL，报 `ModuleNotFoundError: No module named 'server'`

- [ ] **Step 3: Write minimal implementation**

创建 `server.py`：

```python
"""FastAPI app: serves the frontend and the /api/search endpoint."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from crawler import crawl, describe_error
from matcher import extract_title, match_page

SEARCH_BUDGET_SECONDS = 120
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="站内关键词搜索工具")


class SearchRequest(BaseModel):
    startUrl: str
    keywords: list[str] | str

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


@app.post("/api/search")
async def search(req: SearchRequest) -> dict:
    try:
        async with asyncio.timeout(SEARCH_BUDGET_SECONDS):
            crawl_result = await crawl(req.startUrl)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502, f"起始页返回 HTTP {exc.response.status_code}，无法搜索")
    except httpx.TimeoutException:
        raise HTTPException(502, "起始页访问超时，无法搜索")
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, f"起始页无法访问：{describe_error(exc)}")
    except TimeoutError:
        raise HTTPException(504, "搜索总耗时超过 120 秒上限，请缩小范围")

    results = []
    total_hits = 0
    for page in crawl_result.pages:
        hits = match_page(page.html, page.url, req.keywords)
        if not hits:
            continue
        total_hits += len(hits)
        results.append({
            "pageUrl": page.url,
            "pageTitle": extract_title(page.html),
            "hits": [asdict(h) for h in hits],
        })

    return {
        "startUrl": req.startUrl,
        "keywords": req.keywords,
        "pagesCrawled": len(crawl_result.pages),
        "crawledPages": [p.url for p in crawl_result.pages],
        "pagesFailed": crawl_result.failed,
        "totalHits": total_hits,
        "results": results,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="站内关键词搜索工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7100)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/test_server.py -v`
Expected: 除 `test_index_page_served` 外全部通过（前端文件在 Task 4 创建）；整体在 Task 4 后全绿

- [ ] **Step 5: Commit**

```bash
cd /d D:\WebProjects\web_keyword_catcher
git add server.py tests/test_server.py
git commit -m "feat: FastAPI search endpoint with error branches"
```

---

### Task 4: static/index.html — 前端单页面

**Files:**
- Create: `static/index.html`
- Test: `tests/test_server.py::test_index_page_served`（Task 1–3 已写）+ 浏览器手动验证

**Interfaces:**
- Consumes: `POST /api/search` 响应（Task 3 的 JSON 结构）
- Produces: `GET /` 可访问的工具窗口；`kind` 徽章中文映射：
  `text→正文、img-alt→图片alt、img-src→图片文件名、title-attr→title属性、aria-label→aria标签、link-text→链接文本、link-href→链接地址、meta→meta标签、form→表单`

- [ ] **Step 1: Write the page**

创建 `static/index.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>站内关键词搜索</title>
<style>
  :root { --accent:#2f6fed; --bg:#f6f7f9; --card:#fff; --line:#e3e6eb; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Microsoft YaHei",system-ui,sans-serif;
         background:var(--bg); color:#222; }
  header { background:var(--card); border-bottom:1px solid var(--line);
           padding:16px 24px; position:sticky; top:0; z-index:2; }
  h1 { font-size:18px; margin:0 0 12px; }
  .bar { display:flex; gap:8px; flex-wrap:wrap; }
  input { padding:9px 12px; border:1px solid var(--line); border-radius:8px;
          font-size:14px; }
  #url { flex:2 1 320px; } #kw { flex:1 1 200px; }
  button { padding:9px 20px; border:0; border-radius:8px; background:var(--accent);
           color:#fff; font-size:14px; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  main { max-width:960px; margin:24px auto; padding:0 16px 48px; }
  .status { padding:24px; text-align:center; color:#666; }
  .spinner { display:inline-block; width:18px; height:18px; margin-right:8px;
             border:3px solid var(--line); border-top-color:var(--accent);
             border-radius:50%; animation:spin .8s linear infinite;
             vertical-align:-4px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; margin-bottom:16px; overflow:hidden; }
  .card h2 { font-size:15px; margin:0; padding:12px 16px;
             border-bottom:1px solid var(--line); word-break:break-all; }
  .card h2 a { color:var(--accent); text-decoration:none; }
  .hit { display:flex; gap:10px; align-items:flex-start;
         padding:10px 16px; border-top:1px solid #f0f1f4; font-size:14px; }
  .hit:first-of-type { border-top:0; }
  .badge { flex:0 0 auto; font-size:12px; padding:2px 8px; border-radius:20px;
           background:#eef3ff; color:var(--accent); white-space:nowrap; }
  .snippet { flex:1 1 auto; word-break:break-all; line-height:1.6; }
  mark { background:#ffe58f; padding:0 1px; border-radius:2px; }
  .jump { flex:0 0 auto; font-size:12px; color:var(--accent);
          text-decoration:none; padding-top:2px; }
  .linkref { font-size:12px; color:#888; word-break:break-all; }
  .empty { background:#fff8e6; border:1px solid #ffe1a8; border-radius:10px;
           padding:20px 24px; }
  .empty h2 { margin:0 0 10px; font-size:16px; color:#8a5a00; }
  .empty ul { margin:8px 0 0; padding-left:20px; font-size:13px; color:#666; }
  details { font-size:13px; color:#666; margin-top:8px; }
  .error { background:#fdecec; border:1px solid #f5c2c2; color:#a33;
           border-radius:10px; padding:16px 20px; }
  .meta-line { font-size:13px; color:#777; margin:0 0 16px; }
</style>
</head>
<body>
<header>
  <h1>站内关键词搜索</h1>
  <div class="bar">
    <input id="url" type="url" placeholder="起始网址，如 https://example.com">
    <input id="kw" type="text" placeholder="关键词（多个用空格分隔）">
    <button id="go">搜索</button>
  </div>
</header>
<main id="out"></main>
<script>
const KIND_LABELS = {
  "text":"正文","img-alt":"图片alt","img-src":"图片文件名",
  "title-attr":"title属性","aria-label":"aria标签","link-text":"链接文本",
  "link-href":"链接地址","meta":"meta标签","form":"表单"
};
const out = document.getElementById("out");
const btn = document.getElementById("go");

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}
function highlight(snippet, keywords) {
  let html = escapeHtml(snippet);
  for (const kw of keywords) {
    const re = new RegExp(
      kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    html = html.replace(re, m => `<mark>${m}</mark>`);
  }
  return html;
}
function showError(msg) {
  out.innerHTML = `<div class="error">${escapeHtml(msg)}</div>`;
}

async function run() {
  const startUrl = document.getElementById("url").value.trim();
  const keywords = document.getElementById("kw").value.trim().split(/\s+/).filter(Boolean);
  if (!/^https?:\/\/.+/.test(startUrl)) { showError("请输入合法的 http/https 起始网址"); return; }
  if (!keywords.length) { showError("请输入至少一个关键词"); return; }
  btn.disabled = true;
  out.innerHTML = `<div class="status"><span class="spinner"></span>正在抓取页面并搜索，通常需要 10–40 秒…</div>`;
  try {
    const resp = await fetch("/api/search", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({startUrl, keywords})
    });
    const data = await resp.json();
    if (!resp.ok) { showError(data.detail || "搜索失败"); return; }
    render(data);
  } catch (e) {
    showError("无法连接搜索服务：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

function render(data) {
  let html = `<p class="meta-line">共抓取 ${data.pagesCrawled} 个页面，命中 ${data.totalHits} 处。</p>`;
  if (data.totalHits === 0) {
    const kw = data.keywords.map(k => `「${escapeHtml(k)}」`).join("、");
    html += `<div class="empty"><h2>关键词 ${kw} 在已成功抓取的 ${data.pagesCrawled} 个页面中均未出现</h2>
      <div>已抓取的页面：</div><ul>` +
      data.crawledPages.map(u =>
        `<li><a href="${escapeHtml(u)}" target="_blank" rel="noopener">${escapeHtml(u)}</a></li>`
      ).join("") + `</ul></div>`;
  } else {
    for (const page of data.results) {
      html += `<div class="card"><h2><a href="${escapeHtml(page.pageUrl)}" target="_blank" rel="noopener">${
        escapeHtml(page.pageTitle || page.pageUrl)}</a>
        <span class="linkref">${escapeHtml(page.pageUrl)}</span></h2>`;
      for (const hit of page.hits) {
        html += `<div class="hit"><span class="badge">${KIND_LABELS[hit.kind] || hit.kind}</span>
          <span class="snippet">${highlight(hit.snippet, data.keywords)}${
          hit.linkHref ? `<br><span class="linkref">链接：${escapeHtml(hit.linkHref)}</span>` : ""
        }</span><a class="jump" href="${escapeHtml(hit.href)}" target="_blank" rel="noopener">跳转定位</a></div>`;
      }
      html += `</div>`;
    }
  }
  if (data.pagesFailed.length) {
    html += `<details><summary>${data.pagesFailed.length} 个页面抓取失败（不影响结果）</summary><ul>` +
      data.pagesFailed.map(f =>
        `<li>${escapeHtml(f.url)} — ${escapeHtml(f.reason)}</li>`).join("") +
      `</ul></details>`;
  }
  out.innerHTML = html;
}

btn.addEventListener("click", run);
document.getElementById("kw").addEventListener("keydown", e => {
  if (e.key === "Enter") run();
});
</script>
</body>
</html>
```

- [ ] **Step 2: Run the serving test and full suite**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/ -v`
Expected: 全部通过（含 `test_index_page_served`）

- [ ] **Step 3: Commit**

```bash
cd /d D:\WebProjects\web_keyword_catcher
git add static/index.html
git commit -m "feat: single-page frontend with grouped results and jump links"
```

---

### Task 5: package.json 与 dev 启动脚本 + 端到端验证

**Files:**
- Create: `package.json`、`scripts/dev.mjs`、`.gitignore`

**Interfaces:**
- Produces: `npm.cmd run dev [-- --host H --port P]` 启动 Python 服务（默认 127.0.0.1:7100）；停止 dev 进程即停止服务。

- [ ] **Step 1: Write the files**

创建 `package.json`：

```json
{
  "name": "web-keyword-catcher",
  "version": "0.1.0",
  "private": true,
  "description": "站内关键词搜索工具（FastAPI + 原生前端）",
  "scripts": {
    "dev": "node scripts/dev.mjs"
  }
}
```

创建 `scripts/dev.mjs`：

```javascript
// Dev launcher: forward --host/--port CLI args to the Python server.
import { spawn } from "node:child_process";

const args = process.argv.slice(2);
let host = "127.0.0.1";
let port = "7100";
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === "--host" && args[i + 1]) host = args[++i];
  else if (a.startsWith("--host=")) host = a.slice(7);
  else if (a === "--port" && args[i + 1]) port = args[++i];
  else if (a.startsWith("--port=")) port = a.slice(7);
}

const python = process.env.PYTHON || "python";
const child = spawn(python, ["server.py", "--host", host, "--port", port], {
  stdio: "inherit",
  cwd: new URL("..", import.meta.url),
});

child.on("error", (err) => {
  console.error("无法启动 Python 服务：", err.message);
  process.exit(1);
});
child.on("exit", (code) => process.exit(code ?? 0));

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => child.kill(sig));
}
```

创建 `.gitignore`：

```
__pycache__/
.pytest_cache/
*.pyc
node_modules/
```

- [ ] **Step 2: Verify dev server end-to-end**

Run:

```bash
cd /d D:\WebProjects\web_keyword_catcher
npm.cmd run dev -- --port 7123 &
sleep 4
curl -s http://127.0.0.1:7123/ | head -c 200
curl -s -X POST http://127.0.0.1:7123/api/search -H "Content-Type: application/json" -d '{"startUrl":"http://127.0.0.1:7123/","keywords":["关键词"]}'
kill %1
```

Expected: `GET /` 返回页面 HTML（含"站内关键词搜索"）；`POST /api/search` 返回 JSON 且 `totalHits >= 1`（首页含"关键词"字样）；进程被正常终止，无残留。

- [ ] **Step 3: Run full test suite once more**

Run: `cd /d D:\WebProjects\web_keyword_catcher && python -m pytest tests/ -v`
Expected: 全部通过

- [ ] **Step 4: Commit**

```bash
cd /d D:\WebProjects\web_keyword_catcher
git add package.json scripts/dev.mjs .gitignore
git commit -m "chore: npm dev launcher with port forwarding"
```

---

## Self-Review

**1. Spec coverage:**
- §2 形态/选型（FastAPI + 原生前端、httpx + BS4、无数据库、package.json 转发端口）→ Task 3、5 ✅
- §3 架构与接口（GET /、POST /api/search、结果 JSON）→ Task 3 ✅（增加 `crawledPages` 字段以服务 §6 未命中清单，Global Constraints 已注明）
- §4 搜索流程（URL 校验、10s 超时、重定向、UA、30 子页面、并发 8、失败记录）→ Task 2 ✅
- §5 匹配规则（9 种 kind、±60 字符片段、去重、大小写不敏感）→ Task 1 ✅
- §6 前端（输入、不确定进度动画、分组、跳转定位 `#:~:text=`、未命中提示、失败折叠区）→ Task 4 ✅
- §7 异常（前端校验、起始页报错、子页不阻断、120s 上限）→ Task 3、4 ✅
- §8 测试（夹具服务器、各 kind、未命中、端到端、前端手动）→ Task 1–5 ✅

**2. Placeholder scan:** 无 TBD/TODO；所有代码步骤含完整代码。✅

**3. Type consistency:** `match_page(html, page_url, keywords) -> list[Hit]`、`crawl(start_url) -> CrawlResult`、`Hit(kind, snippet, keyword, href, linkHref)`、`CrawlResult.pages/failed` 在 Task 1/2/3 间一致；`describe_error` 在 Task 2 定义、Task 3 导入使用。✅
