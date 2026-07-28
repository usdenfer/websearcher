"""server AI 集成测试：搜索扩展、searchId、缓存（AI 调用全部 mock）。"""
from fastapi.testclient import TestClient

import cache
import server
from server import app

client = TestClient(app)


def setup_function():
    cache._store.clear()


def search(site_server, keywords, depth=1):
    return client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": keywords, "depth": depth})


def test_search_with_expansion(monkeypatch, site_server):
    async def fake_expand(keywords, host):
        assert host.startswith("127.0.0.1")
        return ["beta"]
    monkeypatch.setattr(server, "expand_keywords", fake_expand)

    resp = search(site_server, ["alpha"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["keywords"] == ["alpha"]
    assert data["expandedKeywords"] == ["beta"]
    assert data["searchId"]
    # 扩展词 "beta" 参与匹配：首页 img-alt "Beta diagram" 应命中
    index_page = next(r for r in data["results"]
                      if r["pageUrl"].endswith("/index.html"))
    assert any(h["kind"] == "img-alt" for h in index_page["hits"])
    # 缓存可读且包含页面文本
    entry = cache.get(data["searchId"])
    assert entry and entry["texts"]


def test_search_expand_failure_degrades(monkeypatch, site_server):
    async def fake_expand(keywords, host):
        return []
    monkeypatch.setattr(server, "expand_keywords", fake_expand)

    resp = search(site_server, ["alpha"])
    assert resp.status_code == 200
    assert resp.json()["expandedKeywords"] == []


async def _no_expand(keywords, host):
    return []


async def _fake_stream(messages):
    for t in ["你好", "世界"]:
        yield t


async def _broken_stream(messages):
    from ai import AIError
    raise AIError("未配置 DEEPSEEK_API_KEY")
    yield  # pragma: no cover


def test_summarize_streams(monkeypatch, site_server):
    monkeypatch.setattr(server, "expand_keywords", _no_expand)
    monkeypatch.setattr(server, "chat_stream", _fake_stream)
    sid = search(site_server, ["alpha"]).json()["searchId"]

    resp = client.post("/api/summarize", json={"searchId": sid})
    assert resp.status_code == 200
    assert '"type": "delta", "text": "你好"' in resp.text
    assert '"type": "done"' in resp.text


def test_summarize_unknown_id():
    resp = client.post("/api/summarize", json={"searchId": "nope"})
    assert resp.status_code == 404


def test_summarize_ai_error_event(monkeypatch, site_server):
    monkeypatch.setattr(server, "expand_keywords", _no_expand)
    monkeypatch.setattr(server, "chat_stream", _broken_stream)
    sid = search(site_server, ["alpha"]).json()["searchId"]

    resp = client.post("/api/summarize", json={"searchId": sid})
    assert resp.status_code == 200  # SSE 内错误事件
    assert '"type": "error"' in resp.text
    assert "DEEPSEEK_API_KEY" in resp.text


def test_ask_streams_and_validates(monkeypatch, site_server):
    monkeypatch.setattr(server, "expand_keywords", _no_expand)
    monkeypatch.setattr(server, "chat_stream", _fake_stream)
    sid = search(site_server, ["alpha"]).json()["searchId"]

    resp = client.post("/api/ask", json={"searchId": sid,
                                         "question": "讲了什么？"})
    assert resp.status_code == 200
    assert '"type": "delta"' in resp.text

    assert client.post("/api/ask", json={"searchId": sid,
                                         "question": "  "}).status_code == 422
    assert client.post("/api/ask", json={"searchId": "nope",
                                         "question": "x"}).status_code == 404
