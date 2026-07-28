"""crawler 渲染模式测试（renderer.render_page 全部 mock，不起浏览器）。"""
import asyncio

import crawler
import httpx
import pytest
import renderer
from crawler import (
    is_render_article_link,
    is_render_discovery_hub,
    prioritize_render_links,
)


BASE = "http://example.test/"
TARGET = "http://example.test/html/20203/1815552813.shtml"
DYNAMIC_HUB = "http://example.test/zfxxgk?id=210620544148"

# 模拟渲染后的站点：首页 → 列表页；列表页翻页发现 a1/a2；静态抽取看不到文章
RENDERED = {
    BASE: ("<html><body>首页</body></html>",
           ["http://example.test/list", "http://example.test/nav"]),
    "http://example.test/list": ("<html><body>栏目列表</body></html>",
                                 ["http://example.test/a1",
                                  "http://example.test/a2"]),
    "http://example.test/nav": ("<html><body>导航页</body></html>", []),
    "http://example.test/a1": ("<html><body>文章一正文</body></html>", []),
    "http://example.test/a2": ("<html><body>文章二正文</body></html>", []),
}


async def _fake_render(url):
    if url not in RENDERED:
        raise renderer.RenderError("HTTP 404")
    return RENDERED[url]


async def _fake_render_result(url):
    html, links = await _fake_render(url)
    return renderer.RenderedPage(html, links, url)


def _crawl(monkeypatch, depth=1, start=BASE):
    monkeypatch.setattr(renderer, "render_page_result", _fake_render_result)
    return asyncio.run(crawler.crawl(start, depth=depth, render=True))


def test_render_crawl_follows_rendered_links(monkeypatch):
    result = _crawl(monkeypatch, depth=2)
    urls = [p.url for p in result.pages]
    assert urls[0] == BASE
    assert "http://example.test/list" in urls
    # 翻页发现的文章页也被抓到
    assert "http://example.test/a1" in urls
    assert "http://example.test/a2" in urls


def test_render_crawl_uses_final_url_as_effective_root(monkeypatch):
    final_home = "https://www.example.test/home/"
    article = "https://www.example.test/home/article"

    async def fake_result(url):
        if url == BASE:
            return renderer.RenderedPage(
                "<html><body>redirected</body></html>",
                [article, "https://evil.test/out"],
                final_home,
            )
        if url == article:
            return renderer.RenderedPage(
                "<html><article>body</article></html>",
                [],
                article,
            )
        raise AssertionError(f"unexpected render request: {url}")

    async def old_api_must_not_drive_crawler(url):
        raise AssertionError(f"legacy render API used for {url}")

    monkeypatch.setattr(
        renderer, "render_page_result", fake_result, raising=False
    )
    monkeypatch.setattr(renderer, "render_page", old_api_must_not_drive_crawler)

    result = asyncio.run(crawler.crawl(BASE, depth=1, render=True))

    assert [page.url for page in result.pages] == [final_home, article]


def test_render_crawl_depth1_only_first_level(monkeypatch):
    result = _crawl(monkeypatch, depth=1)
    urls = [p.url for p in result.pages]
    assert "http://example.test/list" in urls
    assert "http://example.test/a1" not in urls


def test_render_crawl_collects_failures(monkeypatch):
    async def flaky(url):
        if url.endswith("/nav"):
            raise renderer.RenderError("页面加载失败：boom")
        return await _fake_render_result(url)
    monkeypatch.setattr(renderer, "render_page_result", flaky)
    result = asyncio.run(crawler.crawl(BASE, depth=1, render=True))
    assert any(f["url"].endswith("/nav") and "boom" in f["reason"]
               for f in result.failed)


def test_render_crawl_respects_page_cap(monkeypatch):
    result = _crawl(monkeypatch, depth=3)
    assert len(result.pages) <= crawler.RENDER_MAX_PAGES


def test_is_render_discovery_hub_recognizes_query_hubs_and_indexes():
    assert is_render_discovery_hub(
        "https://example.test/zfxxgk?id=210620544148")
    assert is_render_discovery_hub("https://example.test/html/tzgg/index.shtml")
    assert not is_render_discovery_hub(
        "https://example.test/html/20203/1815552813.shtml")
    assert not is_render_discovery_hub("https://example.test/list")


def test_is_render_article_link_recognizes_html_articles_despite_query():
    article = "https://example.test/html/20203/1815552813.shtml"
    assert is_render_article_link(article)
    assert is_render_article_link(f"{article}?zzms=1")
    assert not is_render_article_link("https://example.test/html/tzgg/index.shtml")


def test_prioritize_render_links_orders_discovery_before_articles_stably():
    article = "https://example.test/html/20203/1815552813.shtml"
    index = "https://example.test/html/tzgg/index.shtml"
    query_hub = "https://example.test/zfxxgk?id=210620544148"
    other = "https://example.test/list"

    assert prioritize_render_links([article, index, query_hub, other]) == [
        query_hub, index, article, other]
    second_article = "https://example.test/html/20203/1815552814.shtml"
    assert prioritize_render_links([second_article, article]) == [
        second_article, article]


def test_is_render_discovery_hub_excludes_query_only_root():
    assert not is_render_discovery_hub("https://example.test/?q=1")


def test_prioritize_render_links_treats_trailing_slash_index_as_index():
    trailing_index = "https://example.test/html/tzgg/index.shtml/?q=1"
    query_hub = "https://example.test/zfxxgk?id=210620544148"

    assert is_render_discovery_hub(trailing_index)
    assert prioritize_render_links([trailing_index, query_hub]) == [
        query_hub, trailing_index]


def test_is_render_article_link_ignores_trailing_slashes():
    assert is_render_article_link(
        "https://example.test/html/20203/1815552813.shtml/")


def _dynamic_discovery_crawl(monkeypatch, *, target_on_start=False, depth=1):
    ordinary_start_articles = [
        f"{BASE}html/start/{number}.shtml" for number in range(97)
    ]
    hub_articles = [
        f"{BASE}html/hub/{number}.shtml" for number in range(18)
    ]
    start_links = ordinary_start_articles + [DYNAMIC_HUB]
    if target_on_start:
        start_links.append(TARGET)
    rendered = {
        BASE: ("<html><body>首页</body></html>", start_links),
        DYNAMIC_HUB: ("<html><body>动态栏目</body></html>",
                      hub_articles + [TARGET]),
    }

    async def fake_render(url):
        html, links = rendered.get(
            url, ("<html><body>普通页面</body></html>", [])
        )
        return renderer.RenderedPage(html, links, url)

    async def fake_fetch_html_retry(client, url, attempts=4, base_delay=1.5):
        if url == TARGET:
            return "<html><body>随机正文标记 DYNAMIC-BODY-4821</body></html>"
        return "<html><body>普通文章正文</body></html>"

    monkeypatch.setattr(renderer, "render_page_result", fake_render)
    monkeypatch.setattr(
        crawler, "_fetch_crawl_html_retry", fake_fetch_html_retry
    )
    return asyncio.run(crawler.crawl(BASE, depth=depth, render=True))


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_render_discovery_fetches_buried_dynamic_hub_articles_at_every_depth(
        monkeypatch, depth):
    result = _dynamic_discovery_crawl(monkeypatch, depth=depth)
    target_pages = [page for page in result.pages if page.url == TARGET]

    assert len(target_pages) == 1
    assert "DYNAMIC-BODY-4821" in target_pages[0].html
    assert len(result.pages) <= crawler.RENDER_MAX_PAGES


def test_render_discovery_deduplicates_target_seen_on_start_and_hub(monkeypatch):
    result = _dynamic_discovery_crawl(monkeypatch, target_on_start=True)

    assert [page.url for page in result.pages].count(TARGET) == 1
    assert len(result.pages) <= crawler.RENDER_MAX_PAGES


def test_render_discovery_hub_remains_bfs_frontier_after_supplement(monkeypatch):
    child = f"{BASE}list"
    rendered = {
        BASE: ("<html><body>首页</body></html>", [DYNAMIC_HUB]),
        DYNAMIC_HUB: ("<html><body>动态栏目</body></html>", [child]),
        child: ("<html><body>列表页</body></html>", []),
    }
    render_calls = []

    async def fake_render(url):
        render_calls.append(url)
        html, links = rendered[url]
        return renderer.RenderedPage(html, links, url)

    monkeypatch.setattr(renderer, "render_page_result", fake_render)
    result = asyncio.run(crawler.crawl(BASE, depth=2, render=True))

    assert child in [page.url for page in result.pages]
    assert render_calls.count(DYNAMIC_HUB) == 1


def test_failed_static_discovery_article_falls_back_to_rendered_bfs(
        monkeypatch):
    start_target = f"{TARGET}#from-start"
    rendered = {
        BASE: ("<html><body>首页</body></html>",
               [DYNAMIC_HUB, start_target]),
        DYNAMIC_HUB: ("<html><body>动态栏目</body></html>", [TARGET]),
        TARGET: ("<html><body>渲染兜底正文</body></html>", []),
    }

    async def fake_render(url):
        html, links = rendered[url]
        return renderer.RenderedPage(html, links, url)

    async def failed_static_fetch(client, url, attempts=4, base_delay=1.5):
        raise httpx.ConnectError("static fetch failed")

    monkeypatch.setattr(renderer, "render_page_result", fake_render)
    monkeypatch.setattr(
        crawler, "_fetch_crawl_html_retry", failed_static_fetch
    )
    result = asyncio.run(crawler.crawl(BASE, depth=1, render=True))
    target_pages = [page for page in result.pages if page.url == TARGET]

    assert len(target_pages) == 1
    assert "渲染兜底正文" in target_pages[0].html
    assert any(failure["url"] == TARGET for failure in result.failed)
