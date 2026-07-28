"""locator.py 与 /api/locate 的测试：高亮定位代理视图。"""
from fastapi.testclient import TestClient

from locator import build_locate_page
from server import app

client = TestClient(app)

RAW = """<html><head>
<meta http-equiv="Content-Security-Policy" content="default-src 'self'">
<script>trackUser()</script>
</head><body onload="init()">
<p onclick="hack()">Visible Alpha text</p>
<script>evil()</script>
</body></html>"""


def test_build_locate_page_injects_toolbar_and_base():
    out = build_locate_page(RAW, "http://h.local/p", "alpha")
    assert 'base href="http://h.local/p"' in out
    assert "__kwloc" in out          # 顶部定位工具栏
    assert "mark.__kw" in out        # 高亮样式
    assert '"alpha"' in out          # 关键词以 JSON 字符串注入脚本


def test_build_locate_page_sanitizes():
    out = build_locate_page(RAW, "http://h.local/p", "alpha")
    assert "trackUser" not in out and "evil()" not in out
    assert "Content-Security-Policy" not in out
    assert 'onload="init()"' not in out and 'onclick="hack()"' not in out
    assert "Visible Alpha text" in out  # 正文内容保留


def test_locate_endpoint(site_server):
    resp = client.get("/api/locate",
                      params={"url": f"{site_server}/index.html",
                              "keyword": "alpha"})
    assert resp.status_code == 200
    body = resp.text
    assert f'base href="{site_server}/index.html"' in body
    assert "__kwloc" in body


def test_locate_rejects_bad_url():
    resp = client.get("/api/locate",
                      params={"url": "ftp://bad", "keyword": "x"})
    assert resp.status_code == 422


def test_locate_rejects_blank_keyword(site_server):
    resp = client.get("/api/locate",
                      params={"url": f"{site_server}/index.html",
                              "keyword": "  "})
    assert resp.status_code == 422


def test_locate_fetch_failure_returns_502(site_server):
    resp = client.get("/api/locate",
                      params={"url": f"{site_server}/missing.html",
                              "keyword": "x"})
    assert resp.status_code == 502
