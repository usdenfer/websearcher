import asyncio
import json
from contextlib import asynccontextmanager

import pytest

import renderer
from discovery.adapters import FreeCmsAdapter


class FakeResponse:
    def __init__(self, url: str, status: int = 200):
        self.url = url
        self.status = status


class FakePage:
    def __init__(
        self,
        *,
        fail_on_evaluate: bool = False,
        navigation_final_urls: list[str] | None = None,
        evaluate_final_url: str | None = None,
    ):
        self.fail_on_evaluate = fail_on_evaluate
        self.navigation_final_urls = list(navigation_final_urls or [])
        self.evaluate_final_url = evaluate_final_url
        self.gotos: list[str] = []
        self.evaluations: list[tuple[str, dict]] = []
        self.closed = False

    async def goto(self, url: str, **_kwargs):
        self.gotos.append(url)
        final_url = (
            self.navigation_final_urls.pop(0)
            if self.navigation_final_urls
            else url
        )
        return FakeResponse(final_url)

    async def evaluate(self, script: str, arg: dict):
        self.evaluations.append((script, arg))
        if self.fail_on_evaluate:
            raise RuntimeError("secret browser exception")
        return {
            "text": json.dumps({"code": 200, "data": []}),
            "url": self.evaluate_final_url or arg["url"],
        }

    async def close(self):
        self.closed = True


def test_freecms_browser_loader_navigates_then_fetches_on_same_page(
    monkeypatch,
):
    async def run():
        from discovery.freecms_browser import freecms_browser_loader

        page = FakePage()

        @asynccontextmanager
        async def page_session():
            try:
                yield page
            finally:
                await page.close()

        monkeypatch.setattr(renderer, "browser_page_session", page_session)
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        spec = adapter.recent_notice_spec()
        assert spec is not None
        policy = adapter.domain_policy(adapter.origin)

        async with freecms_browser_loader(
            adapter.origin, policy=policy
        ) as loader:
            loaded = await loader.load(spec, 3)

        assert page.gotos == [
            "https://www.zycg.gov.cn/",
            (
                "https://www.zycg.gov.cn/freecms/site/"
                "zygjjgzfcgzx/cggg/index.html"
            ),
        ]
        assert loaded is not None
        body, final_url = loaded
        assert json.loads(body) == {"code": 200, "data": []}
        assert final_url.endswith("currPage=3")
        assert len(page.evaluations) == 1
        _script, arg = page.evaluations[0]
        assert "noticeType=1,2,3,31,32,52,57,61" in arg["url"]
        assert arg["headers"] == {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        assert page.closed is True

    asyncio.run(run())


def test_freecms_browser_loader_sanitizes_page_errors_and_closes(
    monkeypatch,
):
    async def run():
        from discovery.freecms_browser import freecms_browser_loader

        page = FakePage(fail_on_evaluate=True)

        @asynccontextmanager
        async def page_session():
            try:
                yield page
            finally:
                await page.close()

        monkeypatch.setattr(renderer, "browser_page_session", page_session)
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        spec = adapter.recent_notice_spec()
        assert spec is not None
        policy = adapter.domain_policy(adapter.origin)

        async with freecms_browser_loader(
            adapter.origin, policy=policy
        ) as loader:
            assert await loader.load(spec, 1) is None

        assert page.closed is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "final_url",
    [
        "https://outside.test/redirected",
        "http://127.0.0.1/private",
    ],
)
def test_freecms_browser_loader_rejects_navigation_outside_policy(
    monkeypatch,
    final_url,
):
    async def run():
        from discovery.freecms_browser import freecms_browser_loader

        page = FakePage(navigation_final_urls=[final_url])

        @asynccontextmanager
        async def page_session():
            try:
                yield page
            finally:
                await page.close()

        monkeypatch.setattr(renderer, "browser_page_session", page_session)
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")

        with pytest.raises(
            renderer.RenderError,
            match="FreeCMS 浏览器回退初始化失败",
        ):
            async with freecms_browser_loader(
                adapter.origin,
                policy=adapter.domain_policy(adapter.origin),
            ):
                pass

        assert page.closed is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "final_url",
    [
        "https://outside.test/api",
        "http://127.0.0.1/private-api",
    ],
)
def test_freecms_browser_loader_rejects_api_redirect_outside_policy(
    monkeypatch,
    final_url,
):
    async def run():
        from discovery.freecms_browser import freecms_browser_loader

        page = FakePage(evaluate_final_url=final_url)

        @asynccontextmanager
        async def page_session():
            try:
                yield page
            finally:
                await page.close()

        monkeypatch.setattr(renderer, "browser_page_session", page_session)
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        spec = adapter.recent_notice_spec()
        assert spec is not None

        async with freecms_browser_loader(
            adapter.origin,
            policy=adapter.domain_policy(adapter.origin),
        ) as loader:
            assert await loader.load(spec, 1) is None

        assert page.closed is True

    asyncio.run(run())


def test_browser_page_session_reuses_shared_browser_and_closes_page(
    monkeypatch,
):
    async def run():
        page = FakePage()

        class FakeBrowser:
            async def new_page(self):
                return page

        browser = FakeBrowser()
        semaphore = asyncio.Semaphore(1)

        async def get_browser_state():
            return browser, semaphore

        async def unexpected_get_browser():
            raise AssertionError("browser_page_session used split state")

        monkeypatch.setattr(
            renderer, "_get_browser_state", get_browser_state, raising=False
        )
        monkeypatch.setattr(renderer, "_get_browser", unexpected_get_browser)
        monkeypatch.setitem(renderer._state, "sem", None)

        async with renderer.browser_page_session() as yielded:
            assert yielded is page

        assert page.closed is True

    asyncio.run(run())


def test_get_browser_initializes_once_for_concurrent_callers(monkeypatch):
    async def run():
        launches = 0
        browsers = []

        class FakeBrowser:
            def is_connected(self):
                return True

            async def new_page(self):
                return FakePage()

        class FakeChromium:
            async def launch(self, **_kwargs):
                nonlocal launches
                launches += 1
                await asyncio.sleep(0)
                browser = FakeBrowser()
                browsers.append(browser)
                return browser

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

            async def stop(self):
                pass

        class FakeStarter:
            async def start(self):
                return FakePlaywright()

        monkeypatch.setattr(
            "playwright.async_api.async_playwright",
            lambda: FakeStarter(),
        )
        for key in ("loop", "pw", "browser", "sem"):
            monkeypatch.setitem(renderer._state, key, None)

        first, second = await asyncio.gather(
            renderer._get_browser(),
            renderer._get_browser(),
        )

        assert launches == 1
        assert len(browsers) == 1
        assert first is second

    asyncio.run(run())
