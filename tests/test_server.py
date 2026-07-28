"""server.py 的接口测试：端到端搜索、未命中、参数校验、起始页失败。"""
import time

from fastapi.testclient import TestClient

import server
from crawler import CrawledPage, CrawlResult
from discovery.engine import DiscoveryRun
from discovery.models import DiscoveryStats
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
        {"url": f"{site_server}/missing.html", "reason": "HTTP 404"}]
    assert data["totalHits"] > 0
    page = next(r for r in data["results"]
                if r["pageUrl"].endswith("/index.html"))
    assert page["pageTitle"] == "Fixture Home"
    hit_kinds = {h["kind"] for h in page["hits"]}
    assert hit_kinds == {"text"}
    for hit in page["hits"]:
        assert set(hit) == {"kind", "snippet", "keyword", "href", "linkHref"}
    text_hit = next(h for h in page["hits"] if h["kind"] == "text")
    assert "#:~:text=" in text_hit["href"]


def test_search_response_contains_discovery_diagnostics(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["alpha"],
        "depth": 1,
        "render": "off",
    })
    assert resp.status_code == 200
    discovery = resp.json()["discovery"]
    assert discovery["profile"]
    assert isinstance(discovery["sourcesTried"], list)
    assert discovery["candidatesFound"] >= discovery["candidatesFetched"]
    assert discovery["elapsedMs"] >= 0
    assert isinstance(discovery["partial"], bool)


def test_search_matches_keyword_only_in_discovered_body(
        monkeypatch, site_server):
    async def fake_expand(keywords, host):
        return []

    async def fake_discover(*args, **kwargs):
        stats = DiscoveryStats(
            profile="generic",
            sources_tried={"sitemap"},
            sources_succeeded={"sitemap"},
            candidates_found=1,
            candidates_fetched=1,
        )
        return DiscoveryRun(
            pages=[CrawledPage(
                url=f"{site_server}/opaque-article.html",
                html=("<html><head><title>普通标题</title></head>"
                      "<body><main>正文中的 BODY-MARK-9184</main></body>"
                      "</html>"),
            )],
            failed=[],
            stats=stats,
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "discover_pages", fake_discover,
                        raising=False)
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["BODY-MARK-9184"],
        "depth": 1,
        "render": "off",
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert any(
        item["pageUrl"].endswith("/opaque-article.html")
        for item in results
    )


def test_api_reserves_budget_for_structured_discovery(
        monkeypatch, site_server):
    crawl_calls = []
    discovery_calls = []

    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        crawl_calls.append(kwargs)
        return CrawlResult(pages=[
            CrawledPage(url=url, html="<html><main>alpha</main></html>")
        ])

    async def fake_discover(*args, **kwargs):
        discovery_calls.append((args, kwargs))
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(profile="generic"),
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover,
                        raising=False)
    before = time.monotonic()
    response = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["alpha"],
        "depth": 3,
        "render": "off",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["pagesCrawled"] <= 120
    assert "discovery" in data
    assert crawl_calls[0]["max_pages"] == server.BASE_BFS_PAGE_BUDGET
    assert crawl_calls[0]["deadline"] >= before
    args, kwargs = discovery_calls[0]
    assert args[0] == f"{site_server}/index.html"
    assert args[1] == ["alpha"]
    assert args[3:5] == (3, "off")
    assert kwargs["timeout_seconds"] == server.SEARCH_BUDGET_SECONDS
    assert kwargs["started_at"] <= crawl_calls[0]["deadline"]


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


def test_depth2_finds_deep_page(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["omega"],
        "depth": 2,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["depth"] == 2
    assert data["totalHits"] >= 1
    assert any(r["pageUrl"].endswith("/deep.html")
               for r in data["results"])


def test_default_depth_is_1(site_server):
    resp = search(site_server, ["omega"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["depth"] == 1
    assert data["totalHits"] == 0


def test_invalid_depth_rejected(site_server):
    for bad in (0, 4):
        resp = client.post("/api/search", json={
            "startUrl": f"{site_server}/index.html",
            "keywords": ["x"], "depth": bad})
        assert resp.status_code == 422


def test_index_page_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "站内关键词搜索" in resp.text


def test_proactor_loop_factory_returns_loop_instance():
    """自定义 loop factory 会被 asyncio.Runner 零参数直接调用，必须返回
    事件循环实例（返回类会导致 reload 子进程启动失败）。"""
    import asyncio
    import sys

    from server import proactor_loop_factory
    loop = proactor_loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.ProactorEventLoop)
    finally:
        loop.close()
