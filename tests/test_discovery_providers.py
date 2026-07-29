import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date

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
    FreeCmsRecentProvider,
    Provider,
    SearchProvider,
    SitemapProvider,
    YunnanCmsProvider,
)


POLICY = DomainPolicy("x.test", frozenset({"x.test"}))


def test_provider_redirect_refuses_target_before_over_budget_request():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(
                302, headers={"location": "/final"}
            )

        budget = BudgetManager(initial_pages=1, max_pages=1)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            loaded = await Provider(
                client, budget, stats, POLICY
            ).get_text("https://x.test/start", limit=10)

        assert loaded is None
        assert requested == ["https://x.test/start"]
        assert budget.used_html_pages == 1
        assert budget.provider_requests == {"unknown": 1}
        assert stats.partial is True

    asyncio.run(run())


def test_provider_redirect_counts_each_hop_and_applies_params_once():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"location": "/final?next=1"}
                )
            return httpx.Response(200, text="<main>done</main>")

        budget = BudgetManager(initial_pages=2, max_pages=2)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            loaded = await Provider(
                client, budget, stats, POLICY
            ).get_text(
                "https://x.test/start",
                limit=10,
                params={"q": "alpha"},
            )

        assert loaded == (
            "<main>done</main>",
            "https://x.test/final?next=1",
        )
        assert requested == [
            "https://x.test/start?q=alpha",
            "https://x.test/final?next=1",
        ]
        assert budget.used_html_pages == 2
        assert budget.provider_requests == {"unknown": 2}
        assert stats.sources_succeeded == {"unknown"}

    asyncio.run(run())


def test_provider_blocks_offsite_redirect_without_requesting_target():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(
                302,
                headers={
                    "location": "https://evil.test/secret?token=leak"
                },
            )

        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            loaded = await Provider(
                client, BudgetManager(), stats, POLICY
            ).get_text("https://x.test/start")

        assert loaded is None
        assert requested == ["https://x.test/start"]
        assert stats.sources_succeeded == set()
        assert any("重定向" in item for item in stats.warnings)
        assert "evil.test" not in " ".join(stats.warnings)
        assert "token" not in " ".join(stats.warnings)

    asyncio.run(run())


def test_non_html_provider_redirect_counts_only_provider_requests():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/sitemap.xml":
                return httpx.Response(
                    302, headers={"location": "/sitemap-final.xml"}
                )
            return httpx.Response(200, text="<urlset/>")

        budget = BudgetManager()
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            loaded = await Provider(
                client, budget, stats, POLICY
            ).get_text(
                "https://x.test/sitemap.xml",
                counts_as_html=False,
                limit=5,
            )

        assert loaded == (
            "<urlset/>",
            "https://x.test/sitemap-final.xml",
        )
        assert budget.used_html_pages == 0
        assert budget.provider_requests == {"unknown": 2}

    asyncio.run(run())


def test_provider_rejects_redirect_without_location():
    async def run():
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(302)
            )
        ) as client:
            loaded = await Provider(
                client, BudgetManager(), stats, POLICY
            ).get_text("https://x.test/start")

        assert loaded is None
        assert stats.sources_succeeded == set()
        assert stats.warnings == ["unknown: 重定向缺少目标"]

    asyncio.run(run())


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


def test_search_provider_follows_deep_pagination_without_cycles():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            page = request.url.params.get("page", "1")
            if page == "1":
                html = """
                <main>
                  <a class="page" href="?q=alpha&page=2">下一页</a>
                </main>
                """
            elif page == "2":
                html = """
                <main>
                  <a class="page" href="?q=alpha">1</a>
                  <a class="page" href="?q=alpha&page=3">下一页</a>
                </main>
                """
            else:
                html = """
                <main>
                  <a class="page" href="?q=alpha&page=2">上一页</a>
                  <article><a href="/article-2026.html">2026</a></article>
                </main>
                """
            return httpx.Response(200, text=html)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await SearchProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                [SearchSpec("site-search", "https://x.test/search", "q")],
            ).discover(["alpha"])

        assert [item.url for item in result] == [
            "https://x.test/article-2026.html"
        ]
        assert [
            httpx.URL(url).params.get("page", "1") for url in requested
        ] == ["1", "2", "3"]

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
                """
                <main>rendered</main>
                <a class="page" href="/next">下一页</a>
                """,
                [
                    "https://x.test/rendered?utm_source=test",
                    "https://x.test/rendered",
                    "https://x.test/next",
                    "https://x.test/news",
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


def test_category_rendering_uses_render_final_url_for_self_and_pagination():
    async def run():
        class FinalUrlFetcher:
            async def fetch_rendered_page(self, url):
                assert url == "https://x.test/redirected/category"
                return __import__(
                    "renderer", fromlist=["RenderedPage"]
                ).RenderedPage(
                    """
                    <a class="page" href="?page=2">下一页</a>
                    """,
                    [
                        "https://x.test/final/category",
                        "https://x.test/final/category?page=2",
                        "article.html",
                    ],
                    "https://x.test/final/category",
                )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<div id='app'></div>",
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await CategoryProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                ["https://x.test/redirected/category"],
                fetcher=FinalUrlFetcher(),
            ).discover([])

        assert [item.url for item in result] == [
            "https://x.test/final/article.html"
        ]

    asyncio.run(run())


def test_category_rendering_excludes_request_static_and_render_self_urls():
    async def run():
        class ThreeStageFetcher:
            async def fetch_rendered_page(self, url):
                assert url == "https://x.test/static/category"
                return __import__(
                    "renderer", fromlist=["RenderedPage"]
                ).RenderedPage(
                    "<main>rendered</main>",
                    [
                        "https://x.test/request/category",
                        "https://x.test/static/category",
                        "https://x.test/render/category",
                        "article.html",
                    ],
                    "https://x.test/render/category",
                )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/request/category":
                return httpx.Response(
                    302,
                    headers={"location": "/static/category"},
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<div id='app'></div>",
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await CategoryProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                ["https://x.test/request/category"],
                fetcher=ThreeStageFetcher(),
            ).discover([])

        assert [item.url for item in result] == [
            "https://x.test/render/article.html"
        ]

    asyncio.run(run())


def test_category_empty_keywords_and_cross_page_duplicates_are_safe():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("page") == "2":
                body = """
                <main><a href="/same">same</a></main>
                <a class="page" href="?page=3">下一页</a>
                """
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


def test_category_provider_follows_deep_pagination_without_cycles():
    async def run():
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            page = request.url.params.get("page", "1")
            if page == "1":
                html = """
                <main><a class="page" href="?page=2">下一页</a></main>
                """
            elif page == "2":
                html = """
                <main>
                  <a class="page" href="?page=1">1</a>
                  <a class="page" href="?page=3">下一页</a>
                </main>
                """
            else:
                html = """
                <main>
                  <a class="page" href="?page=2">上一页</a>
                  <article><a href="/article-2026.html">2026</a></article>
                </main>
                """
            return httpx.Response(200, text=html)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await CategoryProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                ["https://x.test/category?page=1"],
                render_mode="off",
            ).discover(["alpha"])

        assert [item.url for item in result] == [
            "https://x.test/article-2026.html"
        ]
        assert [
            httpx.URL(url).params.get("page", "1") for url in requested
        ] == ["1", "2", "3"]

    asyncio.run(run())


def test_category_unconsumed_pagination_does_not_hide_later_category_root():
    async def run():
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/category-a":
                links = "".join(
                    f'<a class="page" href="/extra-{index}">{index}</a>'
                    for index in range(12)
                )
                return httpx.Response(
                    200,
                    text=(
                        f"<main>{links}"
                        '<a class="page" href="/category-b">下一页</a>'
                        "</main>"
                    ),
                )
            if request.url.path == "/category-b":
                return httpx.Response(
                    200,
                    text=(
                        '<main><article><a href="/article.html">'
                        "来自栏目 B</a></article></main>"
                    ),
                )
            return httpx.Response(200, text="<main></main>")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await CategoryProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                [
                    "https://x.test/category-a",
                    "https://x.test/category-b",
                ],
                render_mode="off",
            ).discover(["alpha"])

        assert requested_paths.count("/category-b") == 1
        assert sum(
            path.startswith("/extra-") for path in requested_paths
        ) == 12
        assert [item.url for item in result] == [
            "https://x.test/article.html"
        ]

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
        assert budget.provider_requests == {}
        assert budget.used_html_pages == 0

    asyncio.run(run())


def test_provider_budget_rejection_does_not_consume_html_budget():
    async def run():
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text="<main/>")

        budget = BudgetManager()
        budget.provider_requests["site-search"] = 10
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
        assert budget.used_html_pages == 0
        assert stats.partial is False

    asyncio.run(run())


def test_freecms_id_detection_uses_exact_query_key():
    async def run():
        rows = [
            {
                "pageUrl": "/grid/view?grid=1&otherid=7",
                "id": "42",
                "title": "grid",
            },
            {
                "pageUrl": "/existing/view?id=99",
                "id": "42",
                "title": "existing",
            },
            {
                "pageUrl": "/fragment/view#frag",
                "id": "42",
                "title": "fragment",
            },
        ]
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=json.dumps({"code": 200, "data": {"rows": rows}}),
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                FreeCmsAdapter("https://x.test/start"),
            ).discover(["alpha"])

        assert [item.url for item in result] == [
            "https://x.test/grid/view?grid=1&id=42&otherid=7",
            "https://x.test/existing/view?id=99",
            "https://x.test/fragment/view?id=42",
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("responses", "expected_success"),
    [
        ([200, 500], True),
        ([500, 200], True),
        ([500, 500], False),
    ],
)
def test_freecms_business_success_is_independent_of_keyword_order(
    responses,
    expected_success,
):
    async def run():
        response_codes = iter(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            code = next(response_codes)
            payload = (
                {"code": 200, "data": {"rows": []}}
                if code == 200
                else {"code": 500, "msg": "rejected"}
            )
            return httpx.Response(200, text=json.dumps(payload))

        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                FreeCmsAdapter("https://x.test/start"),
            ).discover(["first", "second"])

        assert (
            "site-search-api" in stats.sources_succeeded
        ) is expected_success

    asyncio.run(run())


def test_freecms_business_failure_is_not_success_and_later_keyword_continues():
    async def run():
        requested_keywords: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            keyword = request.url.params["title"]
            requested_keywords.append(keyword)
            if keyword == "bad":
                body = {"code": 500, "msg": "business rejected"}
            elif request.url.params["currPage"] != "1":
                body = {"code": 200, "data": {"rows": []}}
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

        assert requested_keywords == ["bad", "good", "good"]
        assert [item.url for item in result] == [
            "https://x.test/notice/view?id=42&kind=x"
        ]
        assert stats.sources_succeeded == {"site-search-api"}
        assert stats.warnings == [
            "site-search-api: FreeCMS 搜索接口业务失败"
        ]
        assert stats.stop_reason is None

    asyncio.run(run())


def test_freecms_secret_business_failure_is_sanitized_and_stops_channel():
    async def run():
        secret = "Bearer secret-token"

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "code": 500,
                        "msg": (
                            f"Authorization: {secret}; "
                            "Cookie: JSESSIONID=secret-cookie"
                        ),
                    }
                ),
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                adapter,
            ).discover(["alpha"])

        assert result == []
        assert stats.warnings == [
            "site-search-api: FreeCMS 搜索接口业务失败"
        ]
        assert secret not in " ".join(stats.warnings)
        assert "secret-cookie" not in " ".join(stats.warnings)
        assert stats.stop_reason == "channel-failure"

    asyncio.run(run())


def test_freecms_http_failure_stops_channel_without_leaking_response():
    async def run():
        secret = "secret-response-body"

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(
                503,
                text=secret,
                headers={"authorization": "Bearer secret-header"},
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                adapter,
            ).discover(["alpha"])

        assert result == []
        assert stats.warnings == ["site-search-api: HTTPStatusError"]
        assert secret not in " ".join(stats.warnings)
        assert "secret-header" not in " ".join(stats.warnings)
        assert stats.stop_reason == "channel-failure"

    asyncio.run(run())


def test_freecms_keyword_search_paginates_to_recent_date_boundary():
    async def run():
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            page = int(request.url.params["currPage"])
            requested_pages.append(page)
            rows = {
                1: [
                    {
                        "pageUrl": "/notice/recent",
                        "id": "20",
                        "title": "alpha recent",
                        "addtimeStr": "2026-07-20 08:00:00",
                    }
                ],
                2: [
                    {
                        "pageUrl": "/notice/old",
                        "id": "28",
                        "title": "alpha old",
                        "addtimeStr": "2026-06-28 08:00:00",
                    }
                ],
            }[page]
            return httpx.Response(
                200,
                text=json.dumps({"code": 200, "data": {"rows": rows}}),
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                adapter,
                today=date(2026, 7, 29),
            ).discover(["alpha"])

        assert requested_pages == [1, 2]
        assert [item.url for item in result] == [
            "https://x.test/notice/recent?id=20"
        ]
        assert result[0].published_date == "2026-07-20"
        assert result[0].source_evidence == ("site-search-api",)

    asyncio.run(run())


def test_freecms_keyword_search_keeps_unknown_date_until_empty_page():
    async def run():
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            page = int(request.url.params["currPage"])
            requested_pages.append(page)
            rows = (
                [
                    {
                        "pageUrl": "/notice/unknown",
                        "title": "alpha unknown",
                        "addtimeStr": "invalid",
                    }
                ]
                if page == 1
                else []
            )
            return httpx.Response(
                200,
                text=json.dumps({"code": 200, "data": {"rows": rows}}),
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                adapter,
                today=date(2026, 7, 29),
            ).discover(["alpha"])

        assert requested_pages == [1, 2]
        assert [item.published_date for item in result] == [None]
        assert stats.unknown_date_candidates == 1

    asyncio.run(run())


def test_freecms_keywords_have_independent_pagination_and_fixed_params():
    async def run():
        api_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            api_requests.append(request)
            keyword = request.url.params["title"]
            page = request.url.params["currPage"]
            rows = (
                [
                    {
                        "pageUrl": f"/notice/{keyword}-{page}",
                        "title": keyword,
                    }
                ]
                if keyword == "first" or page == "1"
                else []
            )
            return httpx.Response(
                200,
                text=json.dumps({"code": 200, "data": {"rows": rows}}),
            )

        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                POLICY,
                adapter,
                today=date(2026, 7, 29),
                max_pages_per_keyword=2,
            ).discover(["first", "second"])

        assert [
            (request.url.params["title"], request.url.params["currPage"])
            for request in api_requests
        ] == [
            ("first", "1"),
            ("first", "2"),
            ("second", "1"),
            ("second", "2"),
        ]
        assert all(
            request.url.params["pageSize"] == "10"
            for request in api_requests
        )
        assert len(result) == 3

    asyncio.run(run())


def test_freecms_cutoff_is_inclusive_and_disorder_disables_early_stop():
    async def run():
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            page = int(request.url.params["currPage"])
            requested_pages.append(page)
            rows = {
                1: [
                    {
                        "pageUrl": "/notice/old",
                        "addtimeStr": "2026-06-28",
                    },
                    {
                        "pageUrl": "/notice/cutoff",
                        "addtimeStr": "2026-06-29",
                    },
                ],
                2: [
                    {
                        "pageUrl": "/notice/new",
                        "addtimeStr": "2026-07-10",
                    }
                ],
                3: [],
            }[page]
            return httpx.Response(
                200,
                text=json.dumps({"code": 200, "data": {"rows": rows}}),
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                adapter,
                today=date(2026, 7, 29),
            ).discover(["alpha"])

        assert requested_pages == [1, 2, 3]
        assert [item.url for item in result] == [
            "https://x.test/notice/cutoff",
            "https://x.test/notice/new",
        ]
        assert [item.published_date for item in result] == [
            "2026-06-29",
            "2026-07-10",
        ]

    asyncio.run(run())


def test_freecms_nonempty_keyword_page_limit_notes_stop():
    async def run():
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/searchAll.do"):
                return httpx.Response(200, text="<html></html>")
            page = int(request.url.params["currPage"])
            requested_pages.append(page)
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "code": 200,
                        "data": {
                            "rows": [
                                {
                                    "pageUrl": f"/notice/{page}",
                                    "addtimeStr": "2026-07-20",
                                }
                            ]
                        },
                    }
                ),
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/start")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                adapter,
                today=date(2026, 7, 29),
                max_pages_per_keyword=2,
            ).discover(["alpha"])

        assert len(result) == 2
        assert requested_pages == [1, 2]
        assert stats.stop_reason == "provider-page-limit"

    asyncio.run(run())


def test_freecms_warms_html_session_before_search_api():
    """中央政府采购网 searchAll.do 需要先访问 HTML 页拿到有效 JSESSIONID，
    否则返回 code=-1「公告列表查询失败」。发现用的新 client 不会继承
    基础爬取的 cookie，因此 API 调用前必须自行预热会话。"""

    async def run():
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            cookie = request.headers.get("cookie", "")
            if request.url.path.endswith("/searchAll.do"):
                if "JSESSIONID=warm" not in cookie:
                    return httpx.Response(
                        200,
                        text=json.dumps(
                            {"code": "-1", "msg": "公告列表查询失败"}
                        ),
                    )
                return httpx.Response(
                    200,
                    text=json.dumps(
                        {
                            "code": "200",
                            "data": [
                                {
                                    "pageUrl": "/notice/1",
                                    "id": "1",
                                    "title": "alpha 公告",
                                }
                            ],
                        }
                    ),
                )
            return httpx.Response(
                200,
                text="<html></html>",
                headers={"set-cookie": "JSESSIONID=warm; Path=/"},
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(),
                stats,
                DomainPolicy(
                    "www.zycg.gov.cn",
                    frozenset({"www.zycg.gov.cn"}),
                ),
                adapter,
            ).discover(["alpha"])

        assert paths[0] != "/freecms/rest/v1/notice/searchAll.do"
        assert "/freecms/rest/v1/notice/searchAll.do" in paths
        assert [item.url for item in result] == [
            "https://www.zycg.gov.cn/notice/1?id=1"
        ]
        assert "site-search-api" in stats.sources_succeeded

    asyncio.run(run())


def test_freecms_recent_stops_at_inclusive_date_boundary():
    async def run():
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/selectInfoMore.do"):
                page = int(request.url.params["currPage"])
                requested_pages.append(page)
                rows = {
                    1: [
                        {
                            "pageUrl": "/notice/today",
                            "title": "today",
                            "addtimeStr": "2026-07-29 08:00:00",
                        },
                        {
                            "pageUrl": "/notice/july",
                            "title": "july",
                            "addtimeStr": "2026-07-01 08:00:00",
                        },
                    ],
                    2: [
                        {
                            "pageUrl": "/notice/cutoff",
                            "title": "cutoff",
                            "addtimeStr": "2026-06-29 08:00:00",
                        },
                        {
                            "pageUrl": "/notice/old",
                            "title": "old",
                            "addtimeStr": "2026-06-28 08:00:00",
                        },
                    ],
                }[page]
                return httpx.Response(
                    200, text=json.dumps({"code": 200, "data": rows})
                )
            return httpx.Response(200, text="<html></html>")

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
            ).discover([])

        assert [item.url for item in result] == [
            "https://www.zycg.gov.cn/notice/today",
            "https://www.zycg.gov.cn/notice/july",
            "https://www.zycg.gov.cn/notice/cutoff",
        ]
        assert [item.published_date for item in result] == [
            "2026-07-29",
            "2026-07-01",
            "2026-06-29",
        ]
        assert all(
            item.source_evidence == ("freecms-recent",)
            and item.score == 75
            for item in result
        )
        assert requested_pages == [1, 2]
        assert stats.recent_window_days == 30
        assert stats.stop_reason == "date-boundary"
        assert stats.sources_succeeded == {"freecms-recent"}

    asyncio.run(run())


def test_freecms_recent_out_of_order_page_disables_boundary_stop():
    async def run():
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/selectInfoMore.do"):
                return httpx.Response(200, text="<html></html>")
            page = int(request.url.params["currPage"])
            requested_pages.append(page)
            rows = {
                1: [
                    {
                        "pageUrl": "/notice/old",
                        "addtimeStr": "2026-06-28",
                    },
                    {
                        "pageUrl": "/notice/new",
                        "addtimeStr": "2026-07-10",
                    },
                ],
                2: [
                    {
                        "pageUrl": "/notice/next",
                        "addtimeStr": "2026-07-09",
                    }
                ],
                3: [],
            }[page]
            return httpx.Response(
                200, text=json.dumps({"code": 200, "data": rows})
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
            ).discover([])

        assert [item.url for item in result] == [
            "https://www.zycg.gov.cn/notice/new",
            "https://www.zycg.gov.cn/notice/next",
        ]
        assert requested_pages == [1, 2, 3]
        assert stats.stop_reason is None

    asyncio.run(run())


def test_freecms_recent_business_failure_is_not_success():
    async def run():
        class FailedBrowserLoader:
            async def load(self, _spec: SearchSpec, _page: int):
                return None

        @asynccontextmanager
        async def browser_loader_factory(_origin: str):
            yield FailedBrowserLoader()

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=(
                    "<html></html>"
                    if not request.url.path.endswith("/selectInfoMore.do")
                    else json.dumps(
                        {"code": -1, "msg": "secret backend detail"}
                    )
                ),
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
                browser_loader_factory=browser_loader_factory,
            ).discover([])

        assert result == []
        assert stats.sources_succeeded == set()
        assert stats.warnings == [
            "freecms-recent: FreeCMS 最近公告接口业务失败",
            "freecms-recent: browser fallback failed",
        ]
        assert stats.stop_reason == "channel-failure"

    asyncio.run(run())


def test_freecms_recent_transport_failure_stops_channel():
    async def run():
        class FailedBrowserLoader:
            async def load(self, _spec: SearchSpec, _page: int):
                return None

        @asynccontextmanager
        async def browser_loader_factory(_origin: str):
            yield FailedBrowserLoader()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/selectInfoMore.do"):
                raise httpx.ConnectError(
                    "secret transport detail",
                    request=request,
                )
            return httpx.Response(200, text="<html></html>")

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
                browser_loader_factory=browser_loader_factory,
            ).discover([])

        assert result == []
        assert stats.warnings == [
            "freecms-recent: ConnectError",
            "freecms-recent: browser fallback failed",
        ]
        assert "secret" not in " ".join(stats.warnings)
        assert stats.stop_reason == "channel-failure"

    asyncio.run(run())


def test_freecms_recent_transport_failure_falls_back_to_browser():
    async def run():
        browser_pages: list[int] = []
        factory_origins: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/selectInfoMore.do"):
                raise httpx.RemoteProtocolError(
                    "secret transport detail",
                    request=request,
                )
            return httpx.Response(200, text="<html></html>")

        class FakeBrowserLoader:
            async def load(self, spec: SearchSpec, page: int):
                browser_pages.append(page)
                rows = (
                    [
                        {
                            "pageUrl": "/notice/current",
                            "title": "current",
                            "addtimeStr": "2026-07-20",
                        }
                    ]
                    if page == 1
                    else [
                        {
                            "pageUrl": "/notice/old",
                            "title": "old",
                            "addtimeStr": "2026-06-20",
                        }
                    ]
                )
                return (
                    json.dumps({"code": 200, "data": rows}),
                    spec.url,
                )

        @asynccontextmanager
        async def browser_loader_factory(origin: str):
            factory_origins.append(origin)
            yield FakeBrowserLoader()

        budget = BudgetManager()
        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                budget,
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
                browser_loader_factory=browser_loader_factory,
            ).discover([])

        assert [item.url for item in result] == [
            "https://www.zycg.gov.cn/notice/current"
        ]
        assert browser_pages == [1, 2]
        assert factory_origins == ["https://www.zycg.gov.cn"]
        assert stats.sources_succeeded == {"freecms-recent"}
        assert stats.stop_reason == "date-boundary"
        assert "channel-failure" not in stats.warnings
        assert "secret" not in " ".join(stats.warnings)
        assert stats.rendered_pages == 2
        assert budget.provider_requests[
            "freecms-recent-browser"
        ] == 4

    asyncio.run(run())


def test_freecms_recent_static_success_does_not_start_browser():
    async def run():
        factory_started = False

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/selectInfoMore.do"):
                return httpx.Response(
                    200,
                    text=json.dumps({"code": 200, "data": []}),
                )
            return httpx.Response(200, text="<html></html>")

        @asynccontextmanager
        async def browser_loader_factory(_origin: str):
            nonlocal factory_started
            factory_started = True
            yield None

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                browser_loader_factory=browser_loader_factory,
            ).discover([])

        assert result == []
        assert factory_started is False
        assert stats.sources_succeeded == {"freecms-recent"}

    asyncio.run(run())


def test_freecms_recent_browser_failure_stops_channel_and_is_sanitized():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/selectInfoMore.do"):
                raise httpx.RemoteProtocolError(
                    "secret static detail",
                    request=request,
                )
            return httpx.Response(200, text="<html></html>")

        @asynccontextmanager
        async def browser_loader_factory(_origin: str):
            raise RuntimeError("secret browser detail")
            yield

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                browser_loader_factory=browser_loader_factory,
            ).discover([])

        assert result == []
        assert stats.sources_succeeded == set()
        assert stats.stop_reason == "channel-failure"
        assert stats.warnings == [
            "freecms-recent: RemoteProtocolError",
            "freecms-recent: browser fallback failed",
        ]
        assert "secret" not in " ".join(stats.warnings)

    asyncio.run(run())


def test_freecms_recent_html_budget_shortage_does_not_start_browser():
    async def run():
        factory_started = False

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/selectInfoMore.do"):
                raise httpx.RemoteProtocolError(
                    "transport failure",
                    request=request,
                )
            return httpx.Response(200, text="<html></html>")

        @asynccontextmanager
        async def browser_loader_factory(_origin: str):
            nonlocal factory_started
            factory_started = True
            yield None

        budget = BudgetManager(initial_pages=1, max_pages=1)
        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                budget,
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                browser_loader_factory=browser_loader_factory,
            ).discover([])

        assert result == []
        assert factory_started is False
        assert budget.used_html_pages == 0
        assert stats.partial is True
        assert stats.stop_reason == "html-page-budget"

    asyncio.run(run())


def test_freecms_recent_successful_empty_page_is_not_channel_failure():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/selectInfoMore.do"):
                return httpx.Response(
                    200,
                    text=json.dumps({"code": 200, "data": []}),
                )
            return httpx.Response(200, text="<html></html>")

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
            ).discover([])

        assert result == []
        assert stats.sources_succeeded == {"freecms-recent"}
        assert stats.stop_reason is None

    asyncio.run(run())


def test_freecms_recent_keeps_unknown_dates_and_uses_fixed_params():
    async def run():
        api_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/selectInfoMore.do"):
                return httpx.Response(200, text="<html></html>")
            api_requests.append(request)
            rows = (
                [
                    {
                        "pageURL": "/notice/unknown?b=2&a=1",
                        "title": "unknown",
                        "addtimeStr": "not-a-date",
                    },
                    {
                        "pageUrl": "/notice/missing",
                        "title": "missing",
                    },
                ]
                if request.url.params["currPage"] == "1"
                else []
            )
            return httpx.Response(
                200, text=json.dumps({"code": "200", "data": rows})
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
            ).discover([])

        assert [item.url for item in result] == [
            "https://www.zycg.gov.cn/notice/unknown?a=1&b=2",
            "https://www.zycg.gov.cn/notice/missing",
        ]
        assert [item.published_date for item in result] == [None, None]
        assert stats.unknown_date_candidates == 2
        assert [request.url.params["currPage"] for request in api_requests] == [
            "1",
            "2",
        ]
        params = api_requests[0].url.params
        assert params["title"] == ""
        assert params["siteId"] == "6f5243ee-d4d9-4b69-abbd-1e40576ccd7d"
        assert params["channel"] == "d0e7c5f4-b93e-4478-b7fe-61110bb47fd5"
        assert params["pageSize"] == "15"
        assert params["implementWay"] == "1"
        assert params["noticeType"] == "1,2,3,31,32,52,57,61"

    asyncio.run(run())


def test_freecms_recent_nonempty_last_page_notes_provider_limit():
    async def run():
        requested_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/selectInfoMore.do"):
                return httpx.Response(200, text="<html></html>")
            requested_pages.append(int(request.url.params["currPage"]))
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "code": 200,
                        "data": [
                            {
                                "pageUrl": f"/notice/{request.url.params['currPage']}",
                                "addtimeStr": "2026-07-20",
                            }
                        ],
                    }
                ),
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
                max_pages=2,
            ).discover([])

        assert len(result) == 2
        assert requested_pages == [1, 2]
        assert stats.stop_reason == "provider-page-limit"

    asyncio.run(run())


def test_freecms_recent_provider_limit_outranks_date_boundary():
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("/selectInfoMore.do"):
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "code": 200,
                        "data": [
                            {
                                "pageUrl": "/notice/old",
                                "addtimeStr": "2026-06-28",
                            }
                        ],
                    }
                ),
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
                max_pages=1,
            ).discover([])

        assert result == []
        assert stats.stop_reason == "provider-page-limit"

    asyncio.run(run())


def test_freecms_recent_unsupported_site_does_not_mark_source_tried():
    async def run():
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200)

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://x.test/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsRecentProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                adapter,
                today=date(2026, 7, 29),
            ).discover([])

        assert result == []
        assert calls == 0
        assert stats.sources_tried == set()
        assert stats.sources_succeeded == set()

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


@pytest.mark.parametrize(
    ("count_body", "expected_warning"),
    [
        ("0", None),
        ("共 0 条", None),
        ("not available", "site-search: invalid count"),
        ("-1", "site-search: invalid count"),
    ],
)
def test_yunnan_zero_or_invalid_count_skips_result_pages(
    count_body,
    expected_warning,
):
    async def run():
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            return httpx.Response(200, text=count_body)

        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await YunnanCmsProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                YunnanCmsAdapter("https://x.test/start"),
            ).discover(["alpha"])

        assert result == []
        assert requested_paths == ["/searchClassCount.aspx"]
        assert stats.sources_succeeded == set()
        assert stats.warnings == (
            [expected_warning] if expected_warning else []
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("page_status", "expected_success"),
    [(503, False), (200, True)],
)
def test_yunnan_success_requires_a_successful_result_page(
    page_status,
    expected_success,
):
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("searchClassCount.aspx"):
                return httpx.Response(200, text="共 1 条")
            return httpx.Response(
                page_status,
                text='<main><a href="/alpha">alpha</a></main>',
            )

        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            await YunnanCmsProvider(
                client,
                BudgetManager(),
                stats,
                POLICY,
                YunnanCmsAdapter("https://x.test/start"),
            ).discover(["alpha"])

        assert (
            "site-search" in stats.sources_succeeded
        ) is expected_success

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


def test_provider_request_is_cancelled_at_shared_deadline():
    cancelled = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return httpx.Response(200, text="<urlset/>")

    async def run():
        stats = DiscoveryStats()
        budget = BudgetManager(timeout_seconds=0.02)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await SitemapProvider(
                client, budget, stats, POLICY, "https://x.test"
            ).discover([])
        assert result == []
        assert stats.partial is True

    asyncio.run(run())
    assert cancelled is True


def test_yunnan_count_accepts_real_json_response():
    """真实 searchClassCount.aspx 返回 JSON
    {"code":0,"data":[{"id":..,"name":..,"Count":N},...]}，
    而不是纯数字；总数为各栏目 Count 之和。"""
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            keyword = request.url.params.get("tags", "")
            if request.url.path.endswith("searchClassCount.aspx"):
                total = 2 if keyword == "alpha" else 0
                body = json.dumps({
                    "msg": "操作成功", "code": 0,
                    "data": [
                        {"id": "629785882780", "name": "机构概况", "Count": 0},
                        {"id": "691102109750", "name": "内设机构", "Count": 0},
                        {"id": "334716501415", "name": "领导班子", "Count": 0},
                        {"id": "170893906916", "name": "直属单位", "Count": 0},
                        {"id": "267380865381", "name": "政务要闻", "Count": 0},
                        {"id": "210620544148", "name": "人事任免", "Count": total},
                    ],
                }, ensure_ascii=False)
                return httpx.Response(200, text=body)
            if keyword == "alpha" and request.url.params.get("page") == "1":
                return httpx.Response(
                    200,
                    text='<main><a href="/alpha-old.html">alpha 旧文</a>'
                         '</main>',
                )
            return httpx.Response(200, text="<main></main>")

        adapter = YunnanCmsAdapter("https://x.test/start")
        stats = DiscoveryStats()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await YunnanCmsProvider(
                client, BudgetManager(), stats, POLICY, adapter,
            ).discover(["alpha", "beta"])

        assert [item.url for item in result] == [
            "https://x.test/alpha-old.html"
        ]
        assert "site-search" in stats.sources_succeeded
        assert not any("invalid count" in w for w in stats.warnings)

    asyncio.run(run())
