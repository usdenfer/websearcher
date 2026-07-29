import asyncio

import httpx
import pytest

from crawler import FetchedHtml, PAGE_TIMEOUT, USER_AGENT
from discovery.fetcher import DiscoveryFetcher, make_client
from discovery.models import BudgetManager, DiscoveryStats, DomainPolicy
from renderer import RenderedPage


POLICY = DomainPolicy("x.test", frozenset({"x.test"}))


def test_fetch_html_consumes_shared_budget_without_second_request():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<main>正文标记</main>",
            )

        budget = BudgetManager(initial_pages=1, max_pages=1)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            fetcher = DiscoveryFetcher(client, budget, stats)
            assert "正文标记" in (
                await fetcher.fetch_html("https://x.test/a") or ""
            )
            assert await fetcher.fetch_html("https://x.test/b") is None

        assert requested == ["https://x.test/a"]
        assert budget.used_html_pages == 1
        assert stats.partial is True

    asyncio.run(run())


def test_fetch_html_cancels_slow_request_at_shared_deadline(monkeypatch):
    cancelled = False

    async def slow_fetch(*args, **kwargs):
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr("discovery.fetcher.fetch_html_retry", slow_fetch)

    async def run():
        async with httpx.AsyncClient() as client:
            budget = BudgetManager(timeout_seconds=0.02)
            stats = DiscoveryStats()
            fetcher = DiscoveryFetcher(client, budget, stats)
            assert await fetcher.fetch_html("https://x.test/slow") is None
            assert stats.partial is True

    asyncio.run(run())
    assert cancelled is True


def test_fetch_html_counts_redirect_hops_before_request():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"location": "/final"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<main>final</main>",
            )

        budget = BudgetManager(initial_pages=1, max_pages=1)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            fetcher = DiscoveryFetcher(client, budget, stats)
            assert await fetcher.fetch_html(
                "https://x.test/start"
            ) is None

        assert requested == ["https://x.test/start"]
        assert budget.used_html_pages == 1
        assert stats.partial is True

    asyncio.run(run())


def test_fetch_html_policy_blocks_offsite_redirect_before_target_request():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.host == "x.test":
                return httpx.Response(
                    302, headers={"location": "https://outside.test/secret"}
                )
            raise AssertionError("站外目标不得发出请求")

        budget = BudgetManager(initial_pages=5, max_pages=5)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            fetcher = DiscoveryFetcher(
                client, budget, stats, policy=POLICY
            )
            assert await fetcher.fetch_html_page(
                "https://x.test/start"
            ) is None

        assert requested == ["https://x.test/start"]
        assert budget.used_html_pages == 1
        assert all("outside.test" not in warning for warning in stats.warnings)

    asyncio.run(run())


def test_fetch_html_page_returns_allowed_redirect_final_url():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/article"})
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<main>正文关键字</main>",
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            fetcher = DiscoveryFetcher(
                client,
                BudgetManager(initial_pages=5, max_pages=5),
                DiscoveryStats(),
                policy=POLICY,
            )
            page = await fetcher.fetch_html_page(
                "https://x.test/start"
            )

        assert page == FetchedHtml(
            "<main>正文关键字</main>", "https://x.test/article"
        )
        assert requested == [
            "https://x.test/start",
            "https://x.test/article",
        ]

    asyncio.run(run())


def test_non_html_is_recorded_without_crashing():
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"x",
            )
        )
        stats = DiscoveryStats()
        async with httpx.AsyncClient(transport=transport) as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            assert await fetcher.fetch_html("https://x.test/a.pdf") is None

        assert any("非 HTML" in warning for warning in stats.warnings)

    asyncio.run(run())


def test_fetch_rendered_shares_budget_and_counts_only_success(monkeypatch):
    async def run():
        calls: list[str] = []

        async def fake_render(url: str):
            calls.append(url)
            return "<main>rendered</main>", ["https://x.test/linked"]

        monkeypatch.setattr("renderer.render_page", fake_render)
        budget = BudgetManager(initial_pages=1, max_pages=1)
        stats = DiscoveryStats()
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, budget, stats)
            result = await fetcher.fetch_rendered("https://x.test/a")
            refused = await fetcher.fetch_rendered("https://x.test/b")

        assert result == (
            "<main>rendered</main>",
            ["https://x.test/linked"],
        )
        assert refused is None
        assert calls == ["https://x.test/a"]
        assert budget.used_html_pages == 1
        assert stats.rendered_pages == 1
        assert stats.partial is True

    asyncio.run(run())


def test_fetch_rendered_page_enforces_policy_and_returns_final_url(monkeypatch):
    async def run():
        attempted: list[str] = []

        async def fake_render_result(
            url: str, *, navigation_allowed, reserve_request
        ):
            attempted.append(url)
            assert navigation_allowed("https://x.test/final")
            assert not navigation_allowed("https://outside.test/secret")
            assert reserve_request()
            return RenderedPage(
                "<main>rendered</main>",
                ["https://x.test/article"],
                "https://x.test/final",
            )

        monkeypatch.setattr(
            "renderer.render_page_result", fake_render_result
        )
        stats = DiscoveryStats()
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(
                client,
                BudgetManager(initial_pages=2, max_pages=2),
                stats,
                policy=POLICY,
            )
            page = await fetcher.fetch_rendered_page(
                "https://x.test/start"
            )
            denied = await fetcher.fetch_rendered_page(
                "https://outside.test/direct"
            )

        assert page == RenderedPage(
            "<main>rendered</main>",
            ["https://x.test/article"],
            "https://x.test/final",
        )
        assert denied is None
        assert attempted == ["https://x.test/start"]
        assert stats.rendered_pages == 1

    asyncio.run(run())


def test_request_error_warning_does_not_leak_exception_message():
    async def run():
        secret = "api_key=do-not-leak"
        url = (
            "https://user:secret@X.test:443/a"
            "?token=SECRET#fragment"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(secret, request=request)

        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            assert await fetcher.fetch_html(url) is None

        assert stats.warnings == ["https://x.test/a: ConnectError"]
        assert secret not in stats.warnings[0]
        assert all(
            sensitive not in stats.warnings[0]
            for sensitive in (
                "user",
                "secret",
                "token",
                "SECRET",
                "fragment",
            )
        )

    asyncio.run(run())


def test_host_semaphore_uses_canonical_host_and_effective_port():
    async def run():
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(
                client,
                BudgetManager(),
                DiscoveryStats(),
                per_host_concurrency=3,
            )
            https_default = fetcher.host_semaphore(
                "https://user:secret@Example.test.:443/a"
            )
            https_implicit = fetcher.host_semaphore(
                "https://example.test/b"
            )
            https_non_default = fetcher.host_semaphore(
                "https://example.test:8443/c"
            )
            http_default = fetcher.host_semaphore("http://example.test:80")
            http_implicit = fetcher.host_semaphore("http://EXAMPLE.test/")
            ipv6_default = fetcher.host_semaphore("https://[::1]:443/a")
            ipv6_implicit = fetcher.host_semaphore("https://[::1]/b")
            ipv6_expanded = fetcher.host_semaphore(
                "https://[0:0:0:0:0:0:0:1]/c"
            )
            unicode_host = fetcher.host_semaphore(
                "https://例子.测试/a"
            )
            punycode_host = fetcher.host_semaphore(
                "https://xn--fsqu00a.xn--0zwm56d/b"
            )
            malformed = fetcher.host_semaphore("http://[invalid")

        assert https_default is https_implicit
        assert http_default is http_implicit
        assert ipv6_default is ipv6_implicit
        assert ipv6_default is ipv6_expanded
        assert unicode_host is punycode_host
        assert https_non_default is not https_default
        assert http_default is not https_default
        assert malformed is fetcher.host_semaphore("http://[invalid")

    asyncio.run(run())


def test_fetch_rendered_records_failure_but_propagates_cancellation(monkeypatch):
    async def run():
        async def failed_render(url: str):
            raise RuntimeError("browser secret")

        monkeypatch.setattr("renderer.render_page", failed_render)
        stats = DiscoveryStats()
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            assert await fetcher.fetch_rendered("https://x.test/fail") is None
        assert stats.warnings == [
            "https://x.test/fail: render RuntimeError"
        ]
        assert "browser secret" not in stats.warnings[0]

        async def cancelled_render(url: str):
            raise asyncio.CancelledError

        monkeypatch.setattr("renderer.render_page", cancelled_render)
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            with pytest.raises(asyncio.CancelledError):
                await fetcher.fetch_rendered("https://x.test/cancel")

    asyncio.run(run())


def test_make_client_uses_crawler_network_defaults():
    async def run():
        client = make_client()
        try:
            assert client.follow_redirects is True
            assert client.headers["User-Agent"] == USER_AGENT
            assert client.timeout.connect == PAGE_TIMEOUT
            assert client.timeout.read == PAGE_TIMEOUT
        finally:
            await client.aclose()

    asyncio.run(run())
