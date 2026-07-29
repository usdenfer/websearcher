"""前端冒烟测试：真实 Chromium 驱动页面，验证搜索与 AI 区渲染。

需要项目 .venv 安装 playwright 且 Chromium 可用；不满足则跳过。
AI 调用（expand_keywords / chat_stream）全部 mock，不访问真实 API。
"""
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn

pytest.importorskip("playwright")

import cache
import server  # noqa: E402


STATIC_INDEX = Path(__file__).parents[1] / "static" / "index.html"


def test_frontend_renders_discovery_diagnostics():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    assert "data.discovery" in html
    assert "sourcesSucceeded" in html
    assert "Array.isArray(discovery.sourcesSucceeded)" in html
    assert "partial" in html
    assert "已达到搜索预算" in html


def test_frontend_escapes_discovery_warnings():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    assert "discovery.warnings" in html
    assert "Array.isArray(discovery.warnings)" in html
    assert "formatDiscoveryWarning" in html
    assert "escapeHtml(formattedWarning)" in html
    assert "escapeHtml(warning)" not in html
    assert ".slice(0, 5)" in html


def test_frontend_labels_recall_strength_and_uses_safe_results_array():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    assert "data.weakHits" in html
    assert '"title-recall":"标题召回"' in html
    assert '"freecms-recent"' in html
    assert 'page.matchStrength === "weak"' in html
    assert "正文命中" in html
    assert "标题召回" in html
    assert "Array.isArray(data.results)" in html
    assert "resultValues.length === 0" in html
    assert "data.totalHits === 0" not in html


def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_ok(),
                                reason="Chromium 不可用")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_server():
    port = _free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port,
                            log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("app_server 未就绪")
    yield base
    srv.should_exit = True
    thread.join(timeout=5)


async def _no_expand(keywords, host):
    return []


async def _fake_stream(messages):
    for t in ["冒烟", "摘要"]:
        yield t


def test_search_and_ai_flow(app_server, site_server, monkeypatch):
    cache._store.clear()
    monkeypatch.setattr(server, "expand_keywords", _no_expand)
    monkeypatch.setattr(server, "chat_stream", _fake_stream)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(app_server + "/")

        # 默认应为自动模式
        assert page.input_value("#render") == "auto"

        # 填写并搜索
        page.fill("#url", f"{site_server}/index.html")
        page.fill("#kw", "alpha")
        page.click("#go")

        # 结果渲染：meta 行 + 命中卡片
        page.wait_for_selector(".meta-line", timeout=30_000)
        meta = page.text_content(".meta-line")
        assert "命中" in meta
        assert page.query_selector_all(".card"), "应有命中卡片"
        assert page.query_selector_all(".badge"), "应有命中类型徽章"

        # AI 区出现（有 searchId），点击生成摘要，流式文本出现
        assert page.is_visible("#aiSection")
        page.click("#sumBtn")
        page.wait_for_function(
            "document.getElementById('sumOut').textContent.includes('冒烟')",
            timeout=10_000)
        assert "摘要" in page.text_content("#sumOut")

        # 问答：输入问题并提问，问题与流式回答都出现在列表里
        page.fill("#qaInput", "这个页面讲了什么？")
        page.click("#askBtn")
        page.wait_for_function(
            "document.getElementById('qaList').textContent.includes('冒烟')",
            timeout=10_000)
        qa_text = page.text_content("#qaList")
        assert "问：这个页面讲了什么？" in qa_text
        assert "摘要" in qa_text

        browser.close()


def test_warning_formatter_hides_sensitive_details(app_server):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(app_server + "/")

        formatted = page.evaluate("""() => [
          formatDiscoveryWarning(null),
          formatDiscoveryWarning({secret: "object-secret"}),
          formatDiscoveryWarning("feed: RuntimeError"),
          formatDiscoveryWarning(
            "https://user:password@example.test/path?token=secret: ConnectError"
          ),
          formatDiscoveryWarning("api: timeout token=secret"),
          formatDiscoveryWarning("secret-token: TimeoutException")
        ]""")

        assert formatted == [
            "",
            "",
            "feed: RuntimeError",
            "discovery: 处理失败",
            "discovery: 请求超时",
            "discovery: TimeoutException",
        ]
        assert "secret" not in " ".join(formatted)
        assert "https://" not in " ".join(formatted)
        browser.close()


def test_only_weak_recall_renders_result_card(app_server):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "**/api/search",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                json={
                    "searchId": "weak-only",
                    "pagesCrawled": 1,
                    "depth": 1,
                    "render": False,
                    "totalHits": 0,
                    "weakHits": 1,
                    "keywords": ["alpha"],
                    "results": [{
                        "pageUrl": "https://example.test/article",
                        "pageTitle": "Alpha 标题召回结果",
                        "matchStrength": "weak",
                        "hits": [{
                            "kind": "title-recall",
                            "keyword": "alpha",
                            "snippet": "Alpha 标题召回结果",
                            "href": "https://example.test/article",
                        }],
                    }],
                    "crawledPages": ["https://example.test/article"],
                    "pagesFailed": [],
                    "discovery": {},
                },
            ),
        )
        page.goto(app_server + "/")
        page.fill("#url", "https://example.test/")
        page.fill("#kw", "alpha")
        page.click("#go")

        page.wait_for_selector(".card", timeout=10_000)
        assert "Alpha 标题召回结果" in page.text_content(".card")
        assert "标题召回" in page.text_content(".card")
        assert "均未出现" not in page.text_content("#out")
        assert "所在页定位" not in page.text_content(".card")
        browser.close()


def test_render_on_api_end_to_end(app_server, site_server, monkeypatch):
    """接口级检查：render=on 走完整渲染抓取链路（真实 Chromium）。"""
    import json
    import urllib.request

    cache._store.clear()
    monkeypatch.setattr(server, "expand_keywords", _no_expand)
    req = urllib.request.Request(
        app_server + "/api/search",
        data=json.dumps({
            "startUrl": f"{site_server}/index.html",
            "keywords": ["alpha"], "depth": 1, "render": "on",
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=120).read())
    assert data["render"] is True
    assert data["totalHits"] >= 1
    assert data["pagesCrawled"] >= 1
