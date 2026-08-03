import asyncio

import httpx
import pytest

import sitesearch
from crawler import CrawlResult


def test_probe_caches_failure_without_raising():
    sitesearch._probe_cache.clear()

    def handler(request):
        raise httpx.ConnectError("private", request=request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            assert await sitesearch.probe(client, "https://x.test") is False
            assert await sitesearch.probe(client, "https://x.test") is False

    asyncio.run(run())
    assert sitesearch._probe_cache == {"https://x.test": False}


def test_collect_pages_returns_empty_for_crawl_and_discovery_failures(
    monkeypatch,
):
    async def crawl_failure(*args, **kwargs):
        raise RuntimeError("private")

    monkeypatch.setattr(sitesearch, "crawl", crawl_failure)
    assert asyncio.run(
        sitesearch.collect_pages("https://x.test", ["x"])
    )[0] == []

    async def empty_crawl(*args, **kwargs):
        return CrawlResult()

    monkeypatch.setattr(sitesearch, "crawl", empty_crawl)
    assert asyncio.run(
        sitesearch.collect_pages("https://x.test", ["x"])
    )[0] == []


def test_collect_pages_propagates_cancellation(monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(sitesearch, "crawl", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sitesearch.collect_pages("https://x.test", ["x"]))
