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
        {"url": f"{site_server}/missing.html", "reason": "HTTP 404"}]
    assert data["totalHits"] > 0
    page = next(r for r in data["results"]
                if r["pageUrl"].endswith("/index.html"))
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
