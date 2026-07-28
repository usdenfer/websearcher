import asyncio
import json

import httpx
import pytest

from discovery.adapters import FreeCmsAdapter, YunnanCmsAdapter
from discovery.fetcher import DiscoveryFetcher
from discovery.models import (
    BudgetManager,
    DiscoveryStats,
    DomainPolicy,
    SearchSpec,
)
from discovery.providers import (
    CategoryProvider,
    FeedProvider,
    FreeCmsApiProvider,
    SearchProvider,
    SitemapProvider,
    YunnanCmsProvider,
)


POLICY = DomainPolicy("x.test", frozenset({"x.test"}))


def test_search_provider_returns_unique_candidates_from_results_and_pagination():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.params.get("page") == "2":
                html = """
                <main><article>
                  <a href="/article.html">重复 alpha</a>
                  <a href="/second.html">第二条 alpha</a>
                </article></main>
                """
            else:
                html = """
                <main><article>
                  <a href="/article.html">标题 alpha</a>
                </article></main>
                <a class="page" href="?q=alpha&page=2">下一页</a>
                """
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=html,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = SearchProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                [
                    SearchSpec(
                        "site-search",
                        "https://x.test/search",
                        "q",
                        "page",
                    ),
                    SearchSpec(
                        "site-search-secondary",
                        "https://x.test/search",
                        "q",
                        "page",
                    ),
                ],
            )
            result = await provider.discover(["alpha"])

        assert [item.url for item in result] == [
            "https://x.test/article.html",
            "https://x.test/second.html",
        ]
        assert len(requested) == 4

    asyncio.run(run())


def test_sitemap_failure_becomes_sanitized_warning():
    async def run():
        secret = "secret-response-body"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text=secret)

        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = SitemapProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                "https://x.test/private?token=secret",
            )
            assert await provider.discover(["alpha"]) == []

        assert stats.sources_tried == {"sitemap"}
        assert stats.sources_succeeded == set()
        assert stats.warnings == ["sitemap: HTTPStatusError"]
        assert secret not in stats.warnings[0]

    asyncio.run(run())


def test_sitemap_and_feed_deduplicate_across_children_and_sources():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/sitemap.xml":
                body = """<sitemapindex>
                  <sitemap><loc>https://x.test/one.xml</loc></sitemap>
                  <sitemap><loc>https://x.test/two.xml</loc></sitemap>
                </sitemapindex>"""
            elif path in {"/one.xml", "/two.xml"}:
                body = """<urlset>
                  <url><loc>https://x.test/shared</loc></url>
                </urlset>"""
            else:
                body = """<rss><channel><item>
                  <title>shared</title>
                  <link>https://x.test/shared</link>
                </item></channel></rss>"""
            return httpx.Response(200, text=body)

        budget = BudgetManager()
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            sitemap = await SitemapProvider(
                client, budget, stats, POLICY, "https://x.test"
            ).discover([])
            feed = await FeedProvider(
                client,
                budget,
                stats,
                POLICY,
                ["https://x.test/a.xml", "https://x.test/b.xml"],
            ).discover([])

        assert [item.url for item in sitemap] == ["https://x.test/shared"]
        assert [item.url for item in feed] == ["https://x.test/shared"]
        assert budget.used_html_pages == 0

    asyncio.run(run())


def test_category_rendering_uses_fetcher_shared_budget_and_normalizes_links(
    monkeypatch,
):
    async def run():
        render_calls: list[str] = []

        async def render_page(url: str):
            render_calls.append(url)
            return (
                "<main>rendered</main>",
                [
                    "https://x.test/rendered?utm_source=test",
                    "https://x.test/rendered",
                    "https://outside.test/no",
                ],
            )

        monkeypatch.setattr("renderer.render_page", render_page)
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<div id='app'></div>")
        )
        budget = BudgetManager(initial_pages=2, max_pages=2)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(transport=transport) as client:
            fetcher = DiscoveryFetcher(client, budget, stats)
            result = await CategoryProvider(
                client,
                budget,
                stats,
                POLICY,
                ["https://x.test/news"],
                fetcher=fetcher,
            ).discover([])

        assert [item.url for item in result] == [
            "https://x.test/rendered"
        ]
        assert render_calls == ["https://x.test/news"]
        assert budget.used_html_pages == 2
        assert stats.rendered_pages == 1

    asyncio.run(run())


def test_category_empty_keywords_and_cross_page_duplicates_are_safe():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("page") == "2":
                body = """<main><a href="/same">same</a></main>"""
            else:
                body = """
                <main><a href="/same">same</a></main>
                <a class="page" href="?page=2">下一页</a>
                """
            return httpx.Response(200, text=body)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await CategoryProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                ["https://x.test/news"],
                render_mode="off",
            ).discover([])

        assert [item.url for item in result] == ["https://x.test/same"]
        assert result[0].keyword == ""

    asyncio.run(run())


def test_provider_budget_rejection_avoids_request_and_marks_html_partial():
    async def run():
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text="<main/>")

        budget = BudgetManager(initial_pages=0, max_pages=0)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await SearchProvider(
                client,
                budget,
                stats,
                POLICY,
                [SearchSpec("site-search", "https://x.test/search", "q")],
            ).discover(["alpha"])

        assert result == []
        assert calls == 0
        assert stats.partial is True
        assert budget.provider_requests == {"site-search": 1}

    asyncio.run(run())


def test_freecms_business_failure_is_not_success_and_later_keyword_continues():
    async def run():
        requested_keywords: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            keyword = request.url.params["title"]
            requested_keywords.append(keyword)
            if keyword == "bad":
                body = {"code": 500, "msg": "business rejected"}
            else:
                body = {
                    "code": 200,
                    "data": {
                        "rows": [
                            {
                                "pageUrl": "/notice/view?kind=x",
                                "id": "42",
                                "title": "good 标题",
                            },
                            {
                                "pageUrl": "/notice/view?kind=x",
                                "id": "42",
                                "title": "duplicate",
                            },
                        ]
                    },
                }
            return httpx.Response(200, text=json.dumps(body))

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client, BudgetManager(), stats, POLICY, adapter
            ).discover(["bad", "good"])

        assert requested_keywords == ["bad", "good"]
        assert [item.url for item in result] == [
            "https://x.test/notice/view?id=42&kind=x"
        ]
        assert stats.sources_succeeded == {"site-search-api"}
        assert stats.warnings == ["site-search-api: business rejected"]

    asyncio.run(run())


def test_yunnan_count_is_requested_for_each_keyword_and_empty_batch_stops():
    async def run():
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            keyword = request.url.params.get("tags", "")
            requests.append((request.url.path, keyword))
            if request.url.path.endswith("searchClassCount.aspx"):
                return httpx.Response(200, text="1")
            if (
                keyword == "alpha"
                and request.url.params.get("page") == "1"
            ):
                return httpx.Response(
                    200,
                    text='<main><a href="/alpha.html">alpha</a></main>',
                )
            return httpx.Response(200, text="<main></main>")

        adapter = YunnanCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await YunnanCmsProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                adapter,
            ).discover(["alpha", "beta"])

        assert [item.url for item in result] == [
            "https://x.test/alpha.html"
        ]
        assert [
            item for item in requests
            if item[0].endswith("searchClassCount.aspx")
        ] == [
            ("/searchClassCount.aspx", "alpha"),
            ("/searchClassCount.aspx", "beta"),
        ]
        assert requests.count(("/searchN.aspx", "beta")) == 1

    asyncio.run(run())


def test_provider_propagates_cancelled_error():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            provider = SitemapProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                "https://x.test",
            )
            with pytest.raises(asyncio.CancelledError):
                await provider.discover([])

    asyncio.run(run())
