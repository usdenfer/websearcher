import asyncio

import httpx

from crawler import FetchedHtml, PageBudgetExhausted, UnsafeRedirect
from discovery.fetcher import (
    DiscoveryFetcher,
    _host_key,
    _sanitize_url,
    _url_parts,
)
from discovery.models import BudgetManager, DiscoveryStats, DomainPolicy


class RateLimiterSpy:
    def __init__(self):
        self.waited = []
        self.rate_limited = []

    async def wait(self, url):
        self.waited.append(url)

    async def report_rate_limited(self, url):
        self.rate_limited.append(url)


def test_url_helpers_reject_invalid_schemes_and_hide_credentials():
    assert _url_parts("ftp://x.test/file") is None
    assert _host_key("http://[bad") == ("<invalid-url>", None)
    assert _sanitize_url("http://[bad") == "<invalid-url>"
    assert _sanitize_url(
        "https://user:secret@x.test:8443/a?q=secret"
    ) == "https://x.test:8443/a"


def test_fetch_html_page_short_circuits_disallowed_and_expired_requests():
    async def run():
        stats = DiscoveryStats()
        policy = DomainPolicy("x.test", frozenset({"x.test"}))
        budget = BudgetManager(timeout_seconds=0)
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, budget, stats, policy=policy)
            assert (
                await fetcher.fetch_html_page("https://outside.test/")
                is None
            )
            assert await fetcher.fetch_html_page("https://x.test/") is None
        assert stats.partial is True

    asyncio.run(run())


def test_fetch_html_page_maps_known_failures_without_leaking_details(
    monkeypatch,
):
    async def run_case(exc):
        async def fail(*args, **kwargs):
            raise exc

        monkeypatch.setattr("discovery.fetcher.fetch_html_retry", fail)
        stats = DiscoveryStats()
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            fetcher._rate_limiter = RateLimiterSpy()
            result = await fetcher.fetch_html_page(
                "https://user:secret@x.test/a?q=secret"
            )
        return result, stats

    for exc, expected in [
        (PageBudgetExhausted("private"), None),
        (UnsafeRedirect("private"), "重定向目标不在允许范围"),
        (ValueError("private"), "非 HTML 内容"),
    ]:
        result, stats = asyncio.run(run_case(exc))
        assert result is None
        assert "secret" not in " ".join(stats.warnings)
        if expected:
            assert expected in stats.warnings[0]
        else:
            assert stats.partial is True


def test_fetch_html_page_reports_429_to_rate_limiter(monkeypatch):
    request = httpx.Request("GET", "https://x.test/a")
    response = httpx.Response(429, request=request)

    async def fail(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "private",
            request=request,
            response=response,
        )

    monkeypatch.setattr("discovery.fetcher.fetch_html_retry", fail)

    async def run():
        stats = DiscoveryStats()
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            limiter = RateLimiterSpy()
            fetcher._rate_limiter = limiter
            assert await fetcher.fetch_html_page("https://x.test/a") is None
        assert limiter.rate_limited == ["https://x.test/a"]
        assert stats.warnings == ["https://x.test/a: HTTPStatusError"]

    asyncio.run(run())


def test_fetch_html_page_rejects_offsite_final_url(monkeypatch):
    async def load(*args, **kwargs):
        return FetchedHtml(
            "<main>ok</main>",
            "https://outside.test/final",
        )

    monkeypatch.setattr("discovery.fetcher.fetch_html_retry", load)

    async def run():
        policy = DomainPolicy("x.test", frozenset({"x.test"}))
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(
                client,
                BudgetManager(),
                DiscoveryStats(),
                policy=policy,
            )
            assert (
                await fetcher.fetch_html_page("https://x.test/start")
                is None
            )

    asyncio.run(run())
