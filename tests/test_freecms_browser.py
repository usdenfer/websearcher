import asyncio
import json
from contextlib import asynccontextmanager

import pytest

import renderer
from discovery.adapters import FreeCmsAdapter


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status


class FakePage:
    def __init__(self, *, fail_on_evaluate: bool = False):
        self.fail_on_evaluate = fail_on_evaluate
        self.gotos: list[str] = []
        self.evaluations: list[tuple[str, dict]] = []
        self.closed = False

    async def goto(self, url: str, **_kwargs):
        self.gotos.append(url)
        return FakeResponse()

    async def evaluate(self, script: str, arg: dict):
        self.evaluations.append((script, arg))
        if self.fail_on_evaluate:
            raise RuntimeError("secret browser exception")
        return {
            "text": json.dumps({"code": 200, "data": []}),
            "url": arg["url"],
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

        async with freecms_browser_loader(adapter.origin) as loader:
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

        async with freecms_browser_loader(adapter.origin) as loader:
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

        async def get_browser():
            return FakeBrowser()

        monkeypatch.setattr(renderer, "_get_browser", get_browser)
        monkeypatch.setitem(renderer._state, "sem", asyncio.Semaphore(1))

        async with renderer.browser_page_session() as yielded:
            assert yielded is page

        assert page.closed is True

    asyncio.run(run())
