# Rendered Discovery Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure body-only keywords in dynamically listed old articles are discoverable from the site homepage at search depths 1, 2, and 3.

**Architecture:** Add pure URL classifiers that put query-backed dynamic section pages and `index.*` list pages ahead of ordinary articles. In rendered crawls, run a bounded, depth-independent discovery supplement: render at most two high-priority section hubs, statically fetch at most 24 article links from each hub, merge them into the crawl result with normalized-URL deduplication, then spend the remaining 60-page budget on the existing BFS.

**Tech Stack:** Python 3.12, asyncio, httpx, Playwright renderer, pytest

---

## File map

- Modify `crawler.py`
  - Classify rendered links as dynamic/list discovery hubs or article pages.
  - Prioritize discovery hubs deterministically.
  - Add the bounded, depth-independent discovery supplement.
  - Keep supplement and ordinary BFS under the existing 60-page cap.
- Modify `tests/test_crawler_render.py`
  - Lock down link priority.
  - Reproduce a dynamic section link buried behind more than 60 homepage links.
  - Verify the body-only target is fetched at depths 1, 2, and 3.
  - Verify URL deduplication and the global page cap.
- Modify `README.md`
  - Clarify that rendered mode reserves a bounded supplement for dynamic section discovery regardless of selected BFS depth.

### Task 1: Render-link classification and priority

**Files:**
- Modify: `tests/test_crawler_render.py`
- Modify: `crawler.py`

- [ ] **Step 1: Write failing classifier tests**

Add imports and tests:

```python
from crawler import (is_render_article_link, is_render_discovery_hub,
                     prioritize_render_links)


def test_render_discovery_hub_classification():
    assert is_render_discovery_hub(
        "https://example.test/zfxxgk?id=210620544148")
    assert is_render_discovery_hub(
        "https://example.test/html/tzgg/index.shtml")
    assert not is_render_discovery_hub(
        "https://example.test/html/20203/1815552813.shtml")
    assert not is_render_discovery_hub("https://example.test/list")


def test_render_article_link_classification():
    assert is_render_article_link(
        "https://example.test/html/20203/1815552813.shtml")
    assert is_render_article_link(
        "https://example.test/html/20203/1815552813.shtml?zzms=1")
    assert not is_render_article_link(
        "https://example.test/html/tzgg/index.shtml")


def test_render_links_prioritize_query_hubs_then_index_pages():
    links = [
        "https://example.test/html/2607/23_44143.shtml",
        "https://example.test/html/tzgg/index.shtml",
        "https://example.test/zfxxgk?id=210620544148",
        "https://example.test/other",
    ]
    assert prioritize_render_links(links) == [
        "https://example.test/zfxxgk?id=210620544148",
        "https://example.test/html/tzgg/index.shtml",
        "https://example.test/html/2607/23_44143.shtml",
        "https://example.test/other",
    ]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_crawler_render.py -q
```

Expected: collection fails because `is_render_article_link`,
`is_render_discovery_hub`, and `prioritize_render_links` do not exist.

- [ ] **Step 3: Implement the minimal pure helpers**

Add to `crawler.py` after `is_binary_url`:

```python
_INDEX_NAMES = {"index.shtml", "index.html", "index.htm"}
_ARTICLE_SUFFIXES = (".shtml", ".html", ".htm")


def is_render_discovery_hub(url: str) -> bool:
    parts = urlsplit(url)
    name = parts.path.rstrip("/").rsplit("/", 1)[-1].lower()
    if name in _INDEX_NAMES:
        return True
    return bool(parts.query and name and "." not in name)


def is_render_article_link(url: str) -> bool:
    name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
    return name not in _INDEX_NAMES and name.endswith(_ARTICLE_SUFFIXES)


def prioritize_render_links(links: list[str]) -> list[str]:
    indexed = list(enumerate(links))

    def priority(item: tuple[int, str]) -> tuple[int, int]:
        index, url = item
        parts = urlsplit(url)
        name = parts.path.rstrip("/").rsplit("/", 1)[-1].lower()
        if parts.query and name and "." not in name:
            rank = 0
        elif name in _INDEX_NAMES:
            rank = 1
        elif is_render_article_link(url):
            rank = 2
        else:
            rank = 3
        return rank, index

    return [url for _index, url in sorted(indexed, key=priority)]
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_crawler_render.py -q
```

Expected: all existing crawler-render tests and the three new classifier tests
pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add crawler.py tests/test_crawler_render.py
git commit -m "feat: prioritize rendered discovery links"
```

### Task 2: Depth-independent rendered discovery supplement

**Files:**
- Modify: `tests/test_crawler_render.py`
- Modify: `crawler.py`

- [ ] **Step 1: Add the failing buried-article fixture**

Extend `tests/test_crawler_render.py` with:

```python
TARGET = "http://example.test/html/20203/1815552813.shtml"
DYNAMIC_HUB = "http://example.test/zfxxgk?id=210620544148"


def _buried_home_links():
    articles = [
        f"http://example.test/html/2607/{i:03d}.shtml"
        for i in range(97)
    ]
    return [*articles, DYNAMIC_HUB]


def _buried_hub_links():
    articles = [
        f"http://example.test/html/2020/{i:03d}.shtml"
        for i in range(18)
    ]
    return [*articles, TARGET]


async def _fake_buried_render(url):
    if url == BASE:
        return "<html><body>首页</body></html>", _buried_home_links()
    if url == DYNAMIC_HUB:
        return "<html><body>人事公开栏目</body></html>", _buried_hub_links()
    return "<html><body>普通渲染页</body></html>", []


async def _fake_buried_fetch(_client, url, attempts=4, base_delay=1.5):
    if url == TARGET:
        return "<html><body>王丹莉 免去培训中心主任职务。</body></html>"
    return f"<html><body>{url}</body></html>"
```

- [ ] **Step 2: Add failing depth, deduplication, and cap tests**

```python
import pytest


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_render_discovery_finds_buried_body_at_every_depth(
        monkeypatch, depth):
    monkeypatch.setattr(renderer, "render_page", _fake_buried_render)
    monkeypatch.setattr(crawler, "fetch_html_retry", _fake_buried_fetch)

    result = asyncio.run(crawler.crawl(BASE, depth=depth, render=True))

    target_pages = [p for p in result.pages if p.url == TARGET]
    assert len(target_pages) == 1
    assert "王丹莉" in target_pages[0].html
    assert len(result.pages) <= crawler.RENDER_MAX_PAGES


def test_render_discovery_deduplicates_supplement_and_bfs(monkeypatch):
    async def duplicate_render(url):
        if url == BASE:
            return "<html>首页</html>", [DYNAMIC_HUB, TARGET]
        if url == DYNAMIC_HUB:
            return "<html>栏目</html>", [TARGET]
        return "<html>正文</html>", []

    monkeypatch.setattr(renderer, "render_page", duplicate_render)
    monkeypatch.setattr(crawler, "fetch_html_retry", _fake_buried_fetch)

    result = asyncio.run(crawler.crawl(BASE, depth=3, render=True))
    assert [p.url for p in result.pages].count(TARGET) == 1
    assert len(result.pages) <= crawler.RENDER_MAX_PAGES
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_crawler_render.py::test_render_discovery_finds_buried_body_at_every_depth `
  tests\test_crawler_render.py::test_render_discovery_deduplicates_supplement_and_bfs -q
```

Expected: the depth-1 parameterization fails because the current renderer only
follows one ordinary BFS level and never expands the buried hub; the target
page is absent.

- [ ] **Step 4: Add bounded supplement constants**

Add beside the other crawler constants:

```python
RENDER_DISCOVERY_HUBS = 2
RENDER_DISCOVERY_ARTICLES_PER_HUB = 24
```

The maximum supplement size is one start page, two hubs, and 48 articles:
51 pages. At least nine pages remain for ordinary BFS under the 60-page cap.

- [ ] **Step 5: Add a static supplement fetch helper inside `_crawl_render`**

After `render_one`, add:

```python
    async def fetch_static(urls: list[str]) -> list[CrawledPage]:
        if not urls:
            return []
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=PAGE_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            async def fetch_one(url: str) -> CrawledPage | None:
                async with sem:
                    try:
                        html = await fetch_html_retry(
                            client, url, attempts=2, base_delay=1.0)
                        return CrawledPage(url=url, html=html)
                    except Exception as exc:  # noqa: BLE001
                        result.failed.append(
                            {"url": url, "reason": describe_error(exc)})
                        return None

            fetched = await asyncio.gather(*(fetch_one(url) for url in urls))
        return [page for page in fetched if page is not None]
```

- [ ] **Step 6: Implement the depth-independent discovery supplement**

Insert before the existing `for _level in range(depth)` loop:

```python
    prioritized_start = prioritize_render_links(start_links)
    hub_urls: list[str] = []
    for link in prioritized_start:
        normalized = usable(link)
        if normalized is None or normalized in visited:
            continue
        if not is_render_discovery_hub(normalized):
            continue
        visited.add(normalized)
        hub_urls.append(normalized)
        if len(hub_urls) >= RENDER_DISCOVERY_HUBS:
            break

    rendered_hubs = await asyncio.gather(*(render_one(url) for url in hub_urls))
    hubs = [item for item in rendered_hubs if item is not None]
    result.pages.extend(page for page, _links in hubs)

    article_urls: list[str] = []
    for _hub_page, links in hubs:
        per_hub = 0
        for link in prioritize_render_links(links):
            normalized = usable(link)
            if normalized is None or normalized in visited:
                continue
            if not is_render_article_link(normalized):
                continue
            visited.add(normalized)
            article_urls.append(normalized)
            per_hub += 1
            if per_hub >= RENDER_DISCOVERY_ARTICLES_PER_HUB:
                break

    remaining = max_pages - len(result.pages)
    article_urls = article_urls[:remaining]
    result.pages.extend(await fetch_static(article_urls))
```

Before the ordinary BFS loop, keep:

```python
    current_level: list[tuple[str, list[str]]] = [(start_url, start_links)]
```

The existing BFS already skips URLs in `visited` and computes
`remaining = max_pages - len(result.pages)`, so it consumes only the budget
left by the supplement.

- [ ] **Step 7: Run crawler-render tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_crawler_render.py -q
```

Expected: all tests pass, including all three depth values, deduplication, the
existing depth semantics fixture, failure collection, and page-cap coverage.

- [ ] **Step 8: Run adjacent crawler and server tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_crawler.py tests\test_crawler_render.py `
  tests\test_server_auto.py tests\test_server.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit Task 2**

```powershell
git add crawler.py tests/test_crawler_render.py
git commit -m "fix: discover dynamic section articles at every depth"
```

### Task 3: Documentation and complete verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the bounded discovery supplement**

In the rendered-mode and old-article sections of `README.md`, add:

```markdown
- **动态栏目发现补充**：渲染搜索会优先探测少量动态栏目/列表入口，并从
  栏目翻页结果中补抓正文；该补充不受深度 1~3 的选择影响，但仍与普通抓取
  共享 60 页和 10 分钟上限。
```

- [ ] **Step 2: Run documentation and diff checks**

Run:

```powershell
rg -n "动态栏目发现补充|共享 60 页" README.md
git diff --check
```

Expected: the new documentation is found and `git diff --check` exits 0.

- [ ] **Step 3: Run the full test suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

Expected: all tests pass. Existing FastAPI deprecation and Windows
Playwright transport-cleanup warnings may remain; there must be no failures.

- [ ] **Step 4: Verify the real target at all depths**

Run a one-off Python verification that temporarily disables AI keyword
expansion, calls `server.search` for:

```python
startUrl = "https://dct.yn.gov.cn/"
keywords = ["王丹莉"]
render = "auto"
depths = [1, 2, 3]
```

For every depth, assert:

```python
target = "https://dct.yn.gov.cn/html/20203/1815552813.shtml"
assert target in result["crawledPages"]
assert any(
    hit["keyword"] == "王丹莉"
    for page in result["results"] if page["pageUrl"] == target
    for hit in page["hits"]
)
```

Expected: all three depths include the target page and its “王丹莉” text hit.

- [ ] **Step 5: Check repository state**

Run:

```powershell
git status --short
git diff --check
```

Expected: only the intended README change remains after Tasks 1 and 2 were
committed, and the diff has no whitespace errors.

- [ ] **Step 6: Commit documentation**

```powershell
git add README.md
git commit -m "docs: explain dynamic section discovery supplement"
```
