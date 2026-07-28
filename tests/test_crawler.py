"""crawler.py 的单元测试：链接提取、规范化、并发抓取与失败记录。"""
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from crawler import (CONCURRENCY, MAX_SUBPAGES, MAX_TOTAL_PAGES, crawl,
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


def test_extract_skips_visited_before_limit(site_server):
    # 已访问链接不应占用每页提取名额，否则排在导航链接后的正文页
    # 在深层抓取时永远轮不到
    html = (FIXTURE_SITE / "index.html").read_text(encoding="utf-8")
    base = f"{site_server}/index.html"
    visited = {f"{site_server}/sub1.html", f"{site_server}/sub2.html"}
    links = extract_same_site_links(html, base, skip=visited)
    assert links == [f"{site_server}/missing.html"]


def test_crawl_collects_pages_and_failures(site_server):
    result = asyncio.run(crawl(f"{site_server}/index.html"))
    urls = {p.url for p in result.pages}
    assert urls == {f"{site_server}/index.html",
                    f"{site_server}/sub1.html",
                    f"{site_server}/sub2.html"}
    assert result.failed == [
        {"url": f"{site_server}/missing.html", "reason": "HTTP 404"}]


def test_crawl_preserves_redirected_home_and_resolves_relative_links(
        redirect_site):
    result = asyncio.run(crawl(redirect_site["start"], depth=1))

    assert [page.url for page in result.pages] == [
        redirect_site["home"],
        redirect_site["article"],
    ]
    assert redirect_site["keyword"] in result.pages[1].html


def test_crawl_rejects_subpage_redirect_outside_effective_root(
        redirect_site):
    result = asyncio.run(crawl(redirect_site["start"], depth=1))

    assert [page.url for page in result.pages] == [
        redirect_site["home"],
        redirect_site["article"],
    ]
    assert result.failed == [{
        "url": redirect_site["escape"],
        "reason": "重定向到站外地址",
    }]


def test_crawl_start_page_failure_raises(site_server):
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(crawl(f"{site_server}/missing.html"))


def test_crawl_depth1_does_not_reach_deep_page(site_server):
    result = asyncio.run(crawl(f"{site_server}/index.html", depth=1))
    urls = {p.url for p in result.pages}
    assert f"{site_server}/deep.html" not in urls


def test_crawl_depth2_reaches_deep_page(site_server):
    result = asyncio.run(crawl(f"{site_server}/index.html", depth=2))
    urls = {p.url for p in result.pages}
    assert f"{site_server}/deep.html" in urls
    assert f"{site_server}/sub1.html" in urls


def test_crawl_respects_max_pages(site_server):
    result = asyncio.run(crawl(f"{site_server}/index.html",
                               depth=3, max_pages=2))
    assert len(result.pages) == 2
    assert result.pages[0].url.endswith("/index.html")


def test_crawl_deadline_keeps_completed_pages(monkeypatch):
    import crawler

    async def fake_fetch(client, url, **kwargs):
        if url.endswith("/index.html"):
            return ("<html><body><a href='/slow.html'>slow</a>"
                    "<a href='/fast.html'>fast</a></body></html>")
        if url.endswith("/slow.html"):
            await asyncio.sleep(1)
        return f"<html><main>{url}</main></html>"

    monkeypatch.setattr(crawler, "fetch_html_retry", fake_fetch)
    started = time.monotonic()
    result = asyncio.run(crawl(
        "https://deadline.test/index.html",
        depth=1,
        deadline=started + 0.1,
    ))
    assert time.monotonic() - started < 0.8
    assert result.pages[0].url.endswith("/index.html")
    assert any(page.url.endswith("/fast.html") for page in result.pages)
    assert not any(page.url.endswith("/slow.html") for page in result.pages)


def test_render_crawl_forwards_max_pages_and_deadline(monkeypatch):
    import crawler

    calls = []

    async def fake_render(url, depth, max_pages, deadline):
        calls.append((url, depth, max_pages, deadline))
        return crawler.CrawlResult()

    monkeypatch.setattr(crawler, "_crawl_render", fake_render)
    deadline = time.monotonic() + 10
    asyncio.run(crawl(
        "https://render.test/",
        depth=3,
        max_pages=17,
        render=True,
        deadline=deadline,
    ))
    assert calls == [("https://render.test/", 3, 17, deadline)]


def test_static_start_page_obeys_deadline_and_cancels(monkeypatch):
    import crawler

    cancelled = []

    async def slow_fetch(client, url, **kwargs):
        try:
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            cancelled.append(url)
            raise
        return "<html><main>late</main></html>"

    monkeypatch.setattr(crawler, "fetch_html_retry", slow_fetch)
    started = time.monotonic()
    result = asyncio.run(crawl(
        "https://slow-static.test/",
        deadline=started + 0.15,
    ))
    assert time.monotonic() - started < 0.3
    assert result.pages == []
    assert result.failed == [{
        "url": "https://slow-static.test/",
        "reason": "搜索截止时间已到",
    }]
    assert cancelled == ["https://slow-static.test/"]


def test_render_start_page_obeys_deadline_and_cancels(monkeypatch):
    import renderer

    cancelled = []

    async def slow_render(url):
        try:
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            cancelled.append(url)
            raise
        return "<html><main>late</main></html>", []

    monkeypatch.setattr(renderer, "render_page", slow_render)
    started = time.monotonic()
    result = asyncio.run(crawl(
        "https://slow-render.test/",
        render=True,
        deadline=started + 0.02,
    ))
    assert time.monotonic() - started < 0.2
    assert result.pages == []
    assert result.failed == [{
        "url": "https://slow-render.test/",
        "reason": "搜索截止时间已到",
    }]
    assert cancelled == ["https://slow-render.test/"]


def test_constants():
    assert MAX_SUBPAGES == 30
    assert CONCURRENCY == 8
    assert MAX_TOTAL_PAGES == 60


def test_deep_pagination_caps():
    """B 方案：渲染翻页与每页子链接配额要足够深，覆盖老旧栏目列表。"""
    import crawler
    import renderer
    assert renderer.MAX_PAGINATION_CLICKS >= 100
    assert crawler.RENDER_SUBPAGE_LINKS >= 60


class _FlakyHandler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"]).BaseHTTPRequestHandler):
    """前 N 次请求直接掐断连接（模拟 WAF 限流），之后正常响应。"""
    failures_left = 2

    def do_GET(self):  # noqa: N802
        if type(self).failures_left > 0:
            type(self).failures_left -= 1
            self.connection.close()
            return
        body = b"<html><body>alpha flaky ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler_cls):
    import http.server, threading
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_crawl_retries_transient_start_page_failure():
    import crawler
    _FlakyHandler.failures_left = 2
    server = _serve(_FlakyHandler)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
        result = asyncio.run(crawler.crawl(url, depth=1))
        assert any("alpha flaky ok" in p.html for p in result.pages)
    finally:
        server.shutdown()
        server.server_close()


def test_crawl_retry_gives_up_after_limit():
    import crawler
    _FlakyHandler.failures_left = 99  # 一直失败
    server = _serve(_FlakyHandler)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
        with pytest.raises(Exception):
            asyncio.run(crawler.crawl(url, depth=1))
    finally:
        server.shutdown()
        server.server_close()
