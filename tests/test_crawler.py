"""crawler.py 的单元测试：链接提取、规范化、并发抓取与失败记录。"""
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from crawler import (
    CONCURRENCY,
    MAX_SUBPAGES,
    MAX_TOTAL_PAGES,
    FetchedHtml,
    UnsafeRedirect,
    crawl,
    extract_same_site_links,
    fetch_html_response,
    fetch_html_retry,
    is_binary_url,
    normalize_url,
)

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


def test_extract_links_compares_canonical_authority():
    html = """
      <a href="http://EXAMPLE.test/a">implicit default</a>
      <a href="http://example.test.:80/dot">trailing dot</a>
      <a href="http://example.test:81/wrong-port">wrong port</a>
      <a href="http://other.test/out">outside</a>
    """

    links = extract_same_site_links(
        html,
        "http://example.test:80/root/",
    )

    assert "http://example.test/a" in links
    assert "http://example.test.:80/dot" in links
    assert not any("wrong-port" in link for link in links)
    assert not any("other.test" in link for link in links)


def test_extract_respects_limit(site_server):
    html = "".join(f'<a href="/p{i}.html">p{i}</a>' for i in range(50))
    links = extract_same_site_links(html, f"{site_server}/index.html")
    assert len(links) == 50


def test_extract_skips_visited_before_limit(site_server):
    # 已访问链接不应占用每页提取名额，否则排在导航链接后的正文页
    # 在深层抓取时永远轮不到
    html = (FIXTURE_SITE / "index.html").read_text(encoding="utf-8")
    base = f"{site_server}/index.html"
    visited = {f"{site_server}/sub1.html", f"{site_server}/sub2.html"}
    links = extract_same_site_links(html, base, skip=visited)
    assert links == [f"{site_server}/missing.html"]


def test_fetch_html_response_supports_redirect_policy_without_budget_callback():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"location": "/final"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<main>final</main>",
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            result = await fetch_html_response(
                client,
                "https://x.test/start",
                redirect_allowed=lambda url: url.startswith(
                    "https://x.test/"
                ),
            )

        assert result == FetchedHtml(
            "<main>final</main>", "https://x.test/final"
        )
        assert requested == [
            "https://x.test/start",
            "https://x.test/final",
        ]

    asyncio.run(run())


def test_fetch_html_retry_policy_only_blocks_redirect_before_target_request():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.host == "x.test":
                return httpx.Response(
                    302,
                    headers={"location": "https://outside.test/secret"},
                )
            raise AssertionError("站外目标不得发出请求")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            with pytest.raises(UnsafeRedirect):
                await fetch_html_retry(
                    client,
                    "https://x.test/start",
                    attempts=1,
                    include_final_url=True,
                    redirect_allowed=lambda url: url.startswith(
                        "https://x.test/"
                    ),
                )

        assert requested == ["https://x.test/start"]

    asyncio.run(run())


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


def test_crawl_counts_each_redirect_document_request_against_budget(
        redirect_site):
    from discovery.models import BudgetManager

    budget = BudgetManager(initial_pages=1, max_pages=1)
    result = asyncio.run(crawl(
        redirect_site["start"], depth=1, budget=budget,
    ))

    assert budget.used_html_pages == 1
    assert result.pages == []
    assert result.failed == [{
        "url": redirect_site["start"],
        "reason": "页面预算已用尽",
    }]


def test_crawl_rejects_subpage_redirect_outside_effective_root(
        redirect_site):
    redirect_site["handler"].outside_requests = 0
    result = asyncio.run(crawl(redirect_site["start"], depth=1))

    assert [page.url for page in result.pages] == [
        redirect_site["home"],
        redirect_site["article"],
    ]
    assert result.failed == [{
        "url": redirect_site["escape"],
        "reason": "重定向到站外地址",
    }]
    assert redirect_site["handler"].outside_requests == 0


def test_crawl_allows_apex_to_www_but_blocks_unrelated_home_redirect(
        monkeypatch):
    import crawler

    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "example.test":
            return httpx.Response(
                302,
                headers={"Location": "https://www.example.test/home/"},
                request=request,
            )
        if request.url.host == "www.example.test":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<html><main>home</main></html>",
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    transport_box = [transport]
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_client(transport=transport_box[0], **kwargs)

    monkeypatch.setattr(crawler.httpx, "AsyncClient", client_factory)
    result = asyncio.run(crawl("https://example.test/start", depth=0))
    assert result.pages[0].url == "https://www.example.test/home/"
    assert requested == [
        "https://example.test/start",
        "https://www.example.test/home/",
    ]

    requested.clear()

    def hostile_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://evil.test/home/"},
            request=request,
        )

    transport_box[0] = httpx.MockTransport(hostile_handler)
    with pytest.raises(ValueError, match="站外"):
        asyncio.run(crawl("https://example.test/start", depth=0))
    assert requested == ["https://example.test/start"]


def test_crawl_accepts_exact_signature_fetch_double(monkeypatch):
    import crawler

    calls = []

    async def fake_fetch(client, url, attempts, base_delay):
        calls.append((url, attempts, base_delay))
        return crawler.FetchedHtml(
            "<html><main>ok</main></html>",
            "https://www.example.test/home/",
        )

    monkeypatch.setattr(
        crawler,
        "_fetch_crawl_html_retry",
        fake_fetch,
        raising=False,
    )
    result = asyncio.run(crawl("https://example.test/start", depth=0))
    assert result.pages[0].url == "https://www.example.test/home/"
    assert calls == [("https://example.test/start", 4, 1.5)]


def test_crawl_honors_public_exact_signature_fetch_patch(monkeypatch):
    import crawler

    calls = []

    async def fake_fetch(client, url, attempts, base_delay):
        calls.append((url, attempts, base_delay))
        return "<html><main>patched</main></html>"

    monkeypatch.setattr(crawler, "fetch_html_retry", fake_fetch)

    result = asyncio.run(crawl("https://example.test/start", depth=0))

    assert result.pages == [
        crawler.CrawledPage(
            "https://example.test/start",
            "<html><main>patched</main></html>",
        )
    ]
    assert calls == [("https://example.test/start", 4, 1.5)]


def test_crawl_max_pages_counts_redirected_candidate_attempts(monkeypatch):
    import crawler

    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested.append(path)
        if path == "/":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text=(
                    "<html><a href='/a'>a</a><a href='/b'>b</a></html>"
                ),
                request=request,
            )
        if path in {"/a", "/b"}:
            return httpx.Response(
                302,
                headers={"Location": "/same"},
                request=request,
            )
        if path == "/same":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<html><a href='/next'>next</a></html>",
                request=request,
            )
        if path == "/next":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<html>must not be requested</html>",
                request=request,
            )
        raise AssertionError(path)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        crawler.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = asyncio.run(crawl(
        "https://example.test/",
        depth=2,
        max_pages=3,
    ))

    assert [page.url for page in result.pages] == [
        "https://example.test/",
        "https://example.test/same",
    ]
    assert requested.count("/a") == 1
    assert requested.count("/b") == 1
    assert "/next" not in requested


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

    async def fake_fetch(client, url, attempts=4, base_delay=1.5):
        del client, attempts, base_delay
        if url.endswith("/index.html"):
            return ("<html><body><a href='/slow.html'>slow</a>"
                    "<a href='/fast.html'>fast</a></body></html>")
        if url.endswith("/slow.html"):
            await asyncio.sleep(1)
        return f"<html><main>{url}</main></html>"

    monkeypatch.setattr(crawler, "fetch_html_retry", crawler._ORIGINAL_FETCH_HTML_RETRY)
    monkeypatch.setattr(crawler, "_fetch_crawl_html_retry", fake_fetch)
    started = time.monotonic()
    result = asyncio.run(crawl(
        "https://deadline.test/index.html",
        depth=1,
        deadline=started + 0.5,
    ))
    assert time.monotonic() - started < 1.0
    assert result.pages[0].url.endswith("/index.html")
    assert any(page.url.endswith("/fast.html") for page in result.pages)
    assert not any(page.url.endswith("/slow.html") for page in result.pages)


def test_render_crawl_forwards_max_pages_and_deadline(monkeypatch):
    import crawler

    calls = []

    async def fake_render(url, depth, max_pages, deadline, since=None):
        calls.append((url, depth, max_pages, deadline, since))
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
    assert calls == [("https://render.test/", 3, 17, deadline, None)]


def test_static_start_page_obeys_deadline_and_cancels(monkeypatch):
    import crawler

    cancelled = []

    async def slow_fetch(client, url, attempts=4, base_delay=1.5):
        del client, attempts, base_delay
        try:
            await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            cancelled.append(url)
            raise
        return "<html><main>late</main></html>"

    monkeypatch.setattr(crawler, "_fetch_crawl_html_retry", slow_fetch)
    started = time.monotonic()
    result = asyncio.run(crawl(
        "https://slow-static.test/",
        deadline=started + 0.5,
    ))
    assert time.monotonic() - started < 1.0
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

    async def slow_render_result(url, navigation_allowed=None):
        del navigation_allowed
        html, links = await slow_render(url)
        return renderer.RenderedPage(html, links, url)

    monkeypatch.setattr(renderer, "render_page_result", slow_render_result)
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
    assert MAX_SUBPAGES == 5000
    assert isinstance(CONCURRENCY, int) and CONCURRENCY >= 1
    assert MAX_TOTAL_PAGES == 5000


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
