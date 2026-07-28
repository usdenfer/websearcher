"""Threaded HTTP fixture server serving tests/fixtures/site."""
from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


class _DiscoverySiteHandler(http.server.BaseHTTPRequestHandler):
    """Serve a small site with search, category, API and body-only content."""

    keyword = "BODY-ONLY-8472"

    def do_GET(self):
        request = urlsplit(self.path)
        query = parse_qs(request.query)

        if request.path == "/":
            body = """
                <html><head><title>测试门户</title></head><body>
                  <form method="get" action="/search">
                    <label>搜索<input name="q"></label>
                  </form>
                  <script>
                    const endpoint =
                      '/freecms/rest/v1/notice/searchAll.do?title=';
                  </script>
                  <nav>
                    <a href="/navigation.html">站点导航</a>
                  </nav>
                  <main><p>欢迎访问测试门户。</p></main>
                </body></html>
            """
            self._send_html(body)
            return

        if request.path == "/search":
            assert query.get("q") == [self.keyword]
            self._send_html("""
                <html><head><title>搜索结果</title></head><body>
                  <main><div class="result">
                    <a href="/ordinary.html">普通候选</a>
                  </div></main>
                </body></html>
            """)
            return

        if request.path == "/category":
            page = query.get("page", ["1"])[0]
            if page == "1":
                self._send_html("""
                    <html><head><title>公告栏目</title></head><body>
                      <main><a class="page" href="/category?page=2">
                        下一页
                      </a></main>
                    </body></html>
                """)
                return
            if page == "2":
                self._send_html("""
                    <html><head><title>公告栏目第二页</title></head><body>
                      <main><a class="page" href="/category?page=3">
                        下一页
                      </a></main>
                    </body></html>
                """)
                return
            if page == "3":
                self._send_html("""
                    <html><head><title>公告栏目第三页</title></head><body>
                      <main><article>
                        <a href="/deep/article.html">普通公告</a>
                      </article></main>
                    </body></html>
                """)
                return

        if request.path == (
            "/freecms/site/zygjjgzfcgzx/cggg/index.html"
        ):
            self.send_response(302)
            self.send_header("Location", "/category?page=1")
            self.end_headers()
            return

        if request.path in {
            "/api/search",
            "/freecms/rest/v1/notice/searchAll.do",
        }:
            self._send_json({"code": -1, "msg": "业务失败"})
            return

        if request.path == "/deep/article.html":
            self._send_html(f"""
                <html><head><title>普通公告</title></head><body>
                  <nav>站点导航</nav>
                  <article>
                    <p>采购说明中的随机正文标记 {self.keyword}。</p>
                  </article>
                </body></html>
            """)
            return

        if request.path == "/ordinary.html":
            self._send_html("""
                <html><head><title>普通候选</title></head><body>
                  <article><p>这里没有目标正文。</p></article>
                </body></html>
            """)
            return

        if request.path == "/navigation.html":
            self._send_html(f"""
                <html><head><title>导航页</title></head><body>
                  <nav>导航关键词 {self.keyword}</nav>
                  <main><p>此页面没有目标正文。</p></main>
                </body></html>
            """)
            return

        self.send_error(404)

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        del format, args


@pytest.fixture(scope="session")
def discovery_site():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _DiscoverySiteHandler
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _RedirectSiteHandler(http.server.BaseHTTPRequestHandler):
    """Serve same-authority redirects and an attempted cross-host escape."""

    keyword = "REDIRECT-BODY-6421"
    outside_requests = 0

    def do_GET(self):
        request = urlsplit(self.path)
        port = self.server.server_address[1]

        if request.path == "/start":
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{port}/home/",
            )
            self.end_headers()
            return

        if request.path == "/home/":
            self._send_html("""
                <html><head><title>Redirected home</title></head><body>
                  <main>
                    <a href="article">正文页</a>
                    <a href="escape">站外重定向</a>
                  </main>
                </body></html>
            """)
            return

        if request.path == "/home/article":
            self._send_html(f"""
                <html><head><title>Redirected article</title></head><body>
                  <article><p>{self.keyword}</p></article>
                </body></html>
            """)
            return

        if request.path == "/home/escape":
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://localhost:{port}/outside",
            )
            self.end_headers()
            return

        if request.path == "/outside":
            type(self).outside_requests += 1
            self._send_html("<html><main>outside</main></html>")
            return

        self.send_error(404)

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        del format, args


@pytest.fixture(scope="session")
def redirect_site():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _RedirectSiteHandler
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield {
            "start": f"http://127.0.0.1:{port}/start",
            "home": f"http://127.0.0.1:{port}/home/",
            "article": f"http://127.0.0.1:{port}/home/article",
            "escape": f"http://127.0.0.1:{port}/home/escape",
            "keyword": _RedirectSiteHandler.keyword,
            "handler": _RedirectSiteHandler,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
