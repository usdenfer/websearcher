"""server 自动渲染模式测试（crawl 全部 mock，不起浏览器）。"""
from fastapi.testclient import TestClient

import cache
import server
from crawler import CrawledPage, CrawlResult
from discovery import DiscoveryRun, DiscoveryStats
from server import app

client = TestClient(app)

STATIC_HTML = ("<html><body><ul><li><a href='a.html'>文章一</a></li>"
               "<li><a href='b.html'>文章二</a></li></ul></body></html>")
JS_HTML = ("<html><body><div id='c'></div><script>"
           "$.ajax({url: '../../listajax.aspx?PageSize=10'});"
           "</script></body></html>")


def setup_function():
    cache._store.clear()


async def _no_expand(kw, host):
    return []


def _install(monkeypatch, static_html, render_html):
    """mock crawl：按 render 参数返回不同页面，返回调用序列。"""
    calls = []

    async def fake_crawl(url, **kwargs):
        render = bool(kwargs.get("render", False))
        calls.append(render)
        html = render_html if render else static_html
        return CrawlResult(pages=[CrawledPage(url=url, html=html)])

    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "expand_keywords", _no_expand)
    return calls


def _post(site_server, render="auto"):
    return client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["alpha"], "render": render})


def test_auto_escalates_on_zero_hits(monkeypatch, site_server):
    calls = _install(monkeypatch, STATIC_HTML,
                     "<html><body>alpha 命中正文</body></html>")
    resp = _post(site_server)
    assert resp.status_code == 200
    data = resp.json()
    assert calls == [False, True]  # 先静态、后渲染补搜
    assert data["render"] is True
    assert data["totalHits"] >= 1
    assert data["autoNote"]


def test_auto_keeps_static_when_hits_found(monkeypatch, site_server):
    calls = _install(
        monkeypatch,
        "<html><body>alpha alpha alpha alpha 多次命中</body></html>",
        "<html><body>alpha</body></html>")
    data = _post(site_server).json()
    assert calls == [False]  # 静态足够好，不触发渲染
    assert data["render"] is False
    assert data["totalHits"] >= 1


def test_auto_keeps_static_when_render_has_no_more_body_hits(
        monkeypatch, site_server):
    calls = _install(
        monkeypatch,
        JS_HTML.replace("</body>", "<p>alpha</p></body>"),
        "<html><body><p>alpha 正文</p><img src='a.png' alt='other'>"
        "<a href='x.html' title='other'>x</a></body></html>")
    data = _post(site_server).json()
    assert calls == [False, True]
    assert data["render"] is False
    assert data["totalHits"] == 1
    assert data["autoNote"]


def test_auto_keeps_static_when_render_not_better(monkeypatch, site_server):
    calls = _install(monkeypatch, STATIC_HTML, STATIC_HTML)
    data = _post(site_server).json()
    assert calls == [False, True]  # 尝试过渲染
    assert data["render"] is False  # 但没更好，保留静态结果
    assert data["totalHits"] == 0
    assert data["autoNote"]


def test_render_on_off_modes(monkeypatch, site_server):
    calls = _install(monkeypatch, STATIC_HTML, STATIC_HTML)
    assert _post(site_server, render="on").json()["render"] is True
    assert calls == [True]
    calls.clear()
    assert _post(site_server, render="off").json()["render"] is False
    assert calls == [False]


def test_invalid_render_mode_rejected(site_server):
    assert _post(site_server, render="maybe").status_code == 422


def test_auto_static_render_and_discovery_share_budget(
        monkeypatch, site_server):
    budgets = []

    async def fake_crawl(url, **kwargs):
        budget = kwargs["budget"]
        budgets.append(budget)
        for _ in range(30):
            assert budget.reserve_html()
        html = (
            "<html><body>alpha 渲染命中</body></html>"
            if kwargs["render"]
            else JS_HTML
        )
        return CrawlResult(pages=[CrawledPage(url, html)])

    async def fake_discover(*args, **kwargs):
        budget = kwargs["budget"]
        budgets.append(budget)
        assert budget.used_html_pages == 60
        assert budget.expand(high_value_remaining=1)
        attempted = 0
        while budget.reserve_html():
            attempted += 1
        assert attempted == 100
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(
                partial=True, budget_expanded=True
            ),
        )

    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)
    monkeypatch.setattr(server, "expand_keywords", _no_expand)

    response = _post(site_server)

    assert response.status_code == 200
    assert len({id(item) for item in budgets}) == 1
    assert budgets[0].used_html_pages == 5000
