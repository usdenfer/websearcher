"""crawl_archive 归档深扫测试：渲染起始页与栏目列表（含全量翻页），
把所有文章链接的正文静态抓回，覆盖标题索引找不到的正文独有词。"""
from __future__ import annotations

import asyncio
import http.server
import threading

import pytest

import crawler
import renderer
from crawler import crawl_archive


class _FakeRendered:
    def __init__(self, html, links, final_url):
        self.html = html
        self.links = links
        self.final_url = final_url


ARTICLES = {
    "/a1.html": "<html><body>alpha 第一篇</body></html>",
    "/a2.html": "<html><body>正文独有的名字</body></html>",
    "/a3.html": "<html><body>alpha 第三篇 beta</body></html>",
}


class _ArticleHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ARTICLES:
            body = ARTICLES[path].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def article_server():
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _ArticleHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _patch_renderer(monkeypatch, mapping: dict):
    async def fake_render(url, **kwargs):
        if url not in mapping:
            raise renderer.RenderError(f"未注册的渲染地址: {url}")
        html, links = mapping[url]
        return _FakeRendered(html, links, url)
    monkeypatch.setattr(renderer, "render_page_result", fake_render)


def test_archive_fetches_all_article_bodies(monkeypatch, article_server):
    start = f"{article_server}/"
    hub = f"{article_server}/html/tzgg/index.shtml"
    mapping = {
        start: ("<html>首页</html>", [hub, f"{article_server}/img/logo.png",
                                      "https://external.example.com/x.html"]),
        hub: ("<html>列表第1页+第2页拼接</html>",
              [f"{article_server}/a1.html",
               f"{article_server}/a2.html",
               f"{article_server}/a3.html",
               f"{article_server}/a1.html"]),  # 翻页重复链接需去重
    }
    _patch_renderer(monkeypatch, mapping)

    result = asyncio.run(crawl_archive(start))
    urls = sorted(p.url for p in result.pages)
    assert urls == sorted([start, hub,
                           f"{article_server}/a1.html",
                           f"{article_server}/a2.html",
                           f"{article_server}/a3.html"])
    bodies = {p.url: p.html for p in result.pages}
    assert "正文独有的名字" in bodies[f"{article_server}/a2.html"]
    assert "alpha 第一篇" in bodies[f"{article_server}/a1.html"]


def test_archive_start_page_article_links_used_directly(
        monkeypatch, article_server):
    """起始页本身就是栏目列表时，其链接（含翻页收割）直接作为文章源。"""
    start = f"{article_server}/html/tzgg/index.shtml"
    mapping = {
        start: ("<html>列表</html>", [f"{article_server}/a1.html",
                                      f"{article_server}/a3.html"]),
    }
    _patch_renderer(monkeypatch, mapping)

    result = asyncio.run(crawl_archive(start))
    urls = sorted(p.url for p in result.pages)
    assert urls == sorted([start,
                           f"{article_server}/a1.html",
                           f"{article_server}/a3.html"])


def test_archive_render_start_failure_raises(monkeypatch):
    async def boom(url, **kwargs):
        raise renderer.RenderError("页面加载失败：timeout")
    monkeypatch.setattr(renderer, "render_page_result", boom)
    with pytest.raises(ValueError, match="页面加载失败"):
        asyncio.run(crawl_archive("https://x.test/"))


def test_archive_hub_render_failure_is_collected(
        monkeypatch, article_server):
    start = f"{article_server}/"
    bad_hub = f"{article_server}/html/bad/index.shtml"
    mapping = {
        start: ("<html>首页</html>", [bad_hub,
                                      f"{article_server}/a2.html"]),
    }
    _patch_renderer(monkeypatch, mapping)

    result = asyncio.run(crawl_archive(start))
    urls = {p.url for p in result.pages}
    assert f"{article_server}/a2.html" in urls
    assert any(f["url"] == bad_hub for f in result.failed)


def test_archive_https_to_http_downgrade_not_blocked(monkeypatch):
    """同注册域名的 https→http 降级重定向不应被当作站外拦截。

    dct.yn.gov.cn 的栏目页会把 https 降级重定向到 http（端口 443→80），
    按 host+port 比较的守卫会误判为站外，导致首页归档时所有 hub 渲染
    失败、只抓到 91 页 0 命中。
    """
    start = "https://example.gov.cn/"
    hub = "https://example.gov.cn/html/tzgg/index.shtml"
    hub_final = "http://example.gov.cn/html/tzgg/index.shtml"
    article = "https://example.gov.cn/a1.html"

    async def fake_render(url, **kwargs):
        nav = kwargs.get("navigation_allowed")
        if url == start:
            return _FakeRendered("<html>首页</html>", [hub], start)
        if url == hub:
            # 模拟真实渲染器：导航途中遇到降级重定向，先问守卫
            if nav is not None and not nav(hub_final):
                raise renderer.RenderError("重定向到站外地址")
            return _FakeRendered("<html>列表</html>", [article], hub_final)
        raise renderer.RenderError(f"未注册的渲染地址: {url}")

    monkeypatch.setattr(renderer, "render_page_result", fake_render)

    async def fake_fetch(context, url, retries, delay):
        return "<html><body>王丹莉 任职通知</body></html>"
    monkeypatch.setattr(crawler, "_fetch_crawl_html", fake_fetch)

    result = asyncio.run(crawl_archive(start))
    bodies = {p.url: p.html for p in result.pages}
    assert hub_final in bodies
    assert bodies[article] == "<html><body>王丹莉 任职通知</body></html>"


def test_search_api_accepts_archive_mode(site_server, monkeypatch):
    from fastapi.testclient import TestClient
    import server as server_mod

    seen_budgets = []

    async def fake_archive(url, **kw):
        seen_budgets.append(kw.get("budget"))
        return crawler.CrawlResult(pages=[
            crawler.CrawledPage(url=url, html="<html><body>正文独有的名字</body></html>")])

    monkeypatch.setattr(server_mod, "crawl_archive", fake_archive)
    client = TestClient(server_mod.app)
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["正文独有"], "render": "archive"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["renderMode"] == "archive"
    assert data["totalHits"] == 1
    assert "归档深扫" in (data["autoNote"] or "")
    # 归档模式必须给足页面预算，而不是默认的 60 初始页
    assert seen_budgets and seen_budgets[0].page_limit >= 2000


def test_search_api_rejects_bad_render_value(site_server):
    from fastapi.testclient import TestClient
    import server as server_mod
    client = TestClient(server_mod.app)
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["x"], "render": "everything"})
    assert resp.status_code == 422


def test_run_job_archive_mode_uses_archive_fn(tmp_path):
    import jobs
    store = jobs.JobStore(tmp_path / "jobs.json")
    store.add({
        "id": "j1", "name": "", "startUrl": "https://x.test/",
        "keywords": ["alpha"], "depth": 1, "render": "archive",
        "schedule": {"kind": "interval", "hours": 6},
        "enabled": True, "createdAt": "2026-07-29T08:00:00",
        "lastRunAt": None, "prevKeys": [], "lastResult": None,
        "lastError": None, "running": False})

    calls = []

    async def fake_archive(url, **kw):
        calls.append(url)
        return crawler.CrawlResult(pages=[
            crawler.CrawledPage(url=url, html="<html><body>alpha 归档正文</body></html>")])

    async def fake_expand(kw, host):
        return []

    discovery_calls = []

    async def fake_discovery(*args, **kwargs):
        discovery_calls.append(args)
        from discovery import DiscoveryRun
        from discovery.models import DiscoveryStats
        return DiscoveryRun(pages=[], failed=[], stats=DiscoveryStats())

    seen_budgets = []

    async def fake_archive2(url, **kw):
        seen_budgets.append(kw.get("budget"))
        return await fake_archive(url, **kw)

    r = asyncio.run(jobs.run_job(
        store, "j1", expand_fn=fake_expand,
        discovery_fn=fake_discovery, archive_fn=fake_archive2))
    assert calls == ["https://x.test/"]
    assert r["totalHits"] == 1
    assert r["renderUsed"] is True
    assert seen_budgets and seen_budgets[0].page_limit >= 2000
    # 归档模式不跑 discovery（避免在大预算里重复翻页）
    assert discovery_calls == []
