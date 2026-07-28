import asyncio

import httpx
import pytest

from crawler import PAGE_TIMEOUT, USER_AGENT
from discovery.fetcher import DiscoveryFetcher, make_client
from discovery.models import BudgetManager, DiscoveryStats


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
