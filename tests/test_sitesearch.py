"""站点自带搜索接口（sitesearch）的单元测试与集成测试。"""
from __future__ import annotations

import http.server
import json
import threading
from urllib.parse import parse_qs, urlsplit

import asyncio

import pytest

import sitesearch
from crawler import CrawledPage, CrawlResult
from discovery.engine import DiscoveryRun
from discovery.models import DiscoveryStats


ARTICLE_HTML = {
    "/a1.html": "<html><body><p>alpha 第一次出现</p></body></html>",
    "/a2.html": "<html><body><p>alpha 第二次出现 beta</p></body></html>",
    "/a3.html": "<html><body><p>alpha 第三次出现</p></body></html>",
}


def _result_page(links: list[str], pager: str) -> bytes:
    items = "".join(
        f'<a href="{u}" target="_blank">结果标题</a>' for u in links)
    return (f'<html><body><div id="Span_SearchList">{items}'
            f'<a href="#">{pager}</a>'
            f'<a href="javascript:;">下一页</a>'
            f"</div></body></html>").encode("utf-8")


class SearchSiteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/searchClassCount.aspx":
            tags = qs.get("tags", [""])[0]
            count = 3 if tags == "alpha" else 0
            body = json.dumps({"msg": "操作成功", "code": 0, "data": [
                {"id": "1", "name": "通知公告", "Count": count},
            ]}).encode("utf-8")
            self._send(200, body, "application/json")
        elif parsed.path == "/searchN.aspx":
            tags = qs.get("tags", [""])[0]
            page = qs.get("page", ["1"])[0]
            if tags == "alpha" and page == "1":
                body = _result_page(["/a1.html", "/a2.html"], "1 / 2")
            elif tags == "alpha" and page == "2":
                body = _result_page(["/a3.html"], "2 / 2")
            else:
                body = _result_page([], "1 / 0")
            self._send(200, body, "text/html")
        elif parsed.path in ARTICLE_HTML:
            self._send(200, ARTICLE_HTML[parsed.path].encode("utf-8"),
                       "text/html")
        elif parsed.path == "/index.html":
            self._send(
                200,
                (b"<html><body>home, no article links"
                 b"<a href='/searchN.aspx'>site search</a>"
                 b"</body></html>"),
                "text/html",
            )
        else:
            self._send(404, b"not found", "text/html")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture(scope="module")
def search_site():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), SearchSiteHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    sitesearch._probe_cache.clear()
    yield
    sitesearch._probe_cache.clear()


def test_parse_result_links_keeps_same_origin_articles(search_site):
    html = _result_page(["/a1.html", "a2.html",
                         "https://other.example.com/x.html",
                         "javascript:;", "#",
                         "/searchN.aspx?page=2",
                         "/files/pic.png"], "1 / 2").decode("utf-8")
    links = sitesearch.parse_result_links(html, search_site)
    assert links == [f"{search_site}/a1.html", f"{search_site}/a2.html"]


def test_parse_page_count(search_site):
    assert sitesearch.parse_page_count(
        _result_page([], "1 / 7").decode()) == 7
    assert sitesearch.parse_page_count("<html>无分页</html>") == 1


def test_collect_pages_delegates_to_discovery_and_normalizes_skip(monkeypatch):
    calls = {}
    base = CrawlResult(pages=[
        CrawledPage("https://x.test/", "<html>home</html>"),
    ])

    async def fake_crawl(url, **kwargs):
        calls["crawl"] = (url, kwargs)
        return base

    async def fake_discover(url, keywords, base_result, depth, render_mode):
        calls["discover"] = (
            url, keywords, base_result, depth, render_mode,
        )
        return DiscoveryRun(
            pages=[
                CrawledPage(
                    "https://x.test/old/?utm_source=legacy",
                    "<html>alpha</html>",
                ),
                CrawledPage(
                    "https://x.test/new.html",
                    "<html>alpha</html>",
                ),
            ],
            failed=[],
            stats=DiscoveryStats(
                sources_succeeded={"site-search"},
                candidates_found=2,
            ),
        )

    monkeypatch.setattr(sitesearch, "crawl", fake_crawl)
    monkeypatch.setattr(sitesearch, "discover_pages", fake_discover)

    pages, info = asyncio.run(sitesearch.collect_pages(
        "https://x.test/", ["alpha"],
        skip={"https://x.test/old?utm_campaign=seen"},
    ))

    assert calls["crawl"] == (
        "https://x.test/",
        {"depth": 0, "max_pages": 1, "render": False},
    )
    assert calls["discover"] == (
        "https://x.test/", ["alpha"], base, 1, "off",
    )
    assert [page.url for page in pages] == [
        "https://x.test/new.html",
    ]
    assert info == {
        "available": True,
        "linksFound": 2,
        "pagesFetched": 1,
        "deprecated": True,
    }


def test_collect_pages_handles_empty_start_page(monkeypatch):
    async def fake_crawl(*args, **kwargs):
        return CrawlResult(
            failed=[{"url": "https://x.test/", "reason": "empty"}],
        )

    async def should_not_discover(*args, **kwargs):
        raise AssertionError("empty base must not enter discovery")

    monkeypatch.setattr(sitesearch, "crawl", fake_crawl)
    monkeypatch.setattr(
        sitesearch, "discover_pages", should_not_discover,
    )

    pages, info = asyncio.run(
        sitesearch.collect_pages("https://x.test/", ["alpha"])
    )

    assert pages == []
    assert info["available"] is False
    assert info["pagesFetched"] == 0
    assert info["deprecated"] is True


def test_probe_caches_result(search_site, site_server):
    asyncio.run(_test_probe_caches_result(search_site, site_server))


async def _test_probe_caches_result(search_site, site_server):
    import httpx
    async with httpx.AsyncClient() as client:
        assert await sitesearch.probe(client, search_site) is True
        assert await sitesearch.probe(client, search_site) is True
        # 普通静态站没有该接口
        assert await sitesearch.probe(client, site_server) is False


def test_collect_pages_fetches_all_result_articles(search_site):
    asyncio.run(_test_collect_pages_fetches_all_result_articles(search_site))


async def _test_collect_pages_fetches_all_result_articles(search_site):
    pages, info = await sitesearch.collect_pages(
        f"{search_site}/index.html", ["alpha"])
    urls = {p.url for p in pages}
    assert {f"{search_site}/a1.html",
            f"{search_site}/a2.html",
            f"{search_site}/a3.html"} <= urls
    assert info["available"] is True
    assert info["linksFound"] >= 3
    assert info["pagesFetched"] == len(pages)
    assert info["deprecated"] is True


def test_collect_pages_skips_already_crawled(search_site):
    asyncio.run(_test_collect_pages_skips_already_crawled(search_site))


async def _test_collect_pages_skips_already_crawled(search_site):
    pages, info = await sitesearch.collect_pages(
        f"{search_site}/index.html", ["alpha"],
        skip={f"{search_site}/a1.html", f"{search_site}/a2.html"})
    urls = {p.url for p in pages}
    assert f"{search_site}/a1.html" not in urls
    assert f"{search_site}/a2.html" not in urls
    assert f"{search_site}/a3.html" in urls
    assert info["pagesFetched"] == len(pages)


def test_collect_pages_no_keyword_hit(search_site):
    asyncio.run(_test_collect_pages_no_keyword_hit(search_site))


async def _test_collect_pages_no_keyword_hit(search_site):
    pages, info = await sitesearch.collect_pages(
        f"{search_site}/index.html", ["omega"])
    assert not {
        f"{search_site}/a1.html",
        f"{search_site}/a2.html",
        f"{search_site}/a3.html",
    }.intersection(page.url for page in pages)
    assert info["available"] is True
    assert info["pagesFetched"] == len(pages)


def test_collect_pages_plain_site_unavailable(site_server):
    asyncio.run(_test_collect_pages_plain_site_unavailable(site_server))


async def _test_collect_pages_plain_site_unavailable(site_server):
    pages, info = await sitesearch.collect_pages(
        f"{site_server}/index.html", ["alpha"])
    assert pages == []
    assert info["available"] is False
    assert info["pagesFetched"] == 0


def test_search_api_supplements_via_site_search(search_site):
    from fastapi.testclient import TestClient
    import server as server_mod
    client = TestClient(server_mod.app)
    resp = client.post("/api/search", json={
        "startUrl": f"{search_site}/index.html",
        "keywords": ["alpha"], "depth": 1, "render": "off"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["siteSearch"]["available"] is True
    assert data["siteSearch"]["pagesFetched"] >= 3
    assert data["siteSearch"]["deprecated"] is True
    assert "discovery" in data
    urls = {r["pageUrl"] for r in data["results"]}
    assert {f"{search_site}/a1.html", f"{search_site}/a2.html",
            f"{search_site}/a3.html"} <= urls
    assert data["totalHits"] >= 3


def test_run_job_supplements_via_discovery_fn(tmp_path):
    import jobs
    store = jobs.JobStore(tmp_path / "jobs.json")
    store.add({
        "id": "j1", "name": "", "startUrl": "https://x.test/",
        "keywords": ["alpha"], "depth": 1, "render": "off",
        "schedule": {"kind": "interval", "hours": 6},
        "enabled": True, "createdAt": "2026-07-28T08:00:00",
        "lastRunAt": None, "prevKeys": [], "lastResult": None,
        "lastError": None, "running": False})

    from crawler import CrawledPage, CrawlResult

    async def fake_crawl(url, **kw):
        return CrawlResult(pages=[
            CrawledPage(url="https://x.test/", html="<html>无命中</html>")])

    async def fake_expand(kw, host):
        return []

    async def fake_discovery(url, keywords, base_result, depth, render_mode):
        assert base_result.pages[0].url == "https://x.test/"
        return DiscoveryRun(
            pages=[CrawledPage(
                url="https://x.test/old.html",
                html="<html><main>alpha 旧文</main></html>",
            )],
            failed=[],
            stats=DiscoveryStats(),
        )

    r = asyncio.run(jobs.run_job(
        store, "j1", crawl_fn=fake_crawl, expand_fn=fake_expand,
        discovery_fn=fake_discovery))
    assert r["totalHits"] == 1
    assert r["top"][0]["pageUrl"] == "https://x.test/old.html"
