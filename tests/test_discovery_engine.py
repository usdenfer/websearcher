from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from crawler import CrawledPage, CrawlResult
from discovery.engine import (
    DiscoveryRun,
    discover_pages,
    merge_candidates,
    rank_candidates,
)
from discovery.models import Candidate, DomainPolicy, SearchSpec


def test_merge_normalizes_urls_keeps_best_score_and_stable_ties():
    first_tie = Candidate(
        "HTTPS://X.TEST/a/?b=2&a=1#top",
        "feed",
        score=80,
    )
    higher = Candidate(
        "https://x.test/a?a=1&b=2",
        "site-search",
        score=100,
    )
    lower = Candidate(
        "https://x.test/a/?a=1&b=2&utm_source=test",
        "sitemap",
        score=55,
    )

    merged = merge_candidates([first_tie, lower, higher])
    assert merged == [
        Candidate(
            "https://x.test/a?a=1&b=2",
            "site-search",
            score=100,
        )
    ]

    equal_score = Candidate(
        "https://x.test/a/?a=1&b=2",
        "category",
        score=80,
    )
    tied = merge_candidates([first_tie, equal_score])
    assert tied[0].source == "feed"


def test_merge_skips_invalid_normalized_urls():
    assert merge_candidates(
        [
            Candidate("", "feed", score=99),
            Candidate("https://x.test/good", "sitemap", score=55),
        ]
    ) == [Candidate("https://x.test/good", "sitemap", score=55)]


def test_rank_prioritizes_score_and_enforces_source_and_section_limits():
    items = [
        Candidate(
            "https://x.test/n1.html",
            "category",
            score=60,
            section="news",
        ),
        Candidate(
            "https://x.test/n2.html",
            "site-search",
            score=100,
            keyword="alpha",
            section="news",
        ),
        Candidate(
            "https://x.test/g1.html",
            "sitemap",
            score=55,
            section="guide",
        ),
        Candidate(
            "https://x.test/g2.html",
            "sitemap",
            score=54,
            section="guide",
        ),
        Candidate(
            "https://x.test/n3.html",
            "category",
            score=50,
            section="news",
        ),
    ]

    ranked = rank_candidates(items, per_source=1, per_section=2)

    assert [item.url for item in ranked] == [
        "https://x.test/n2.html",
        "https://x.test/n1.html",
        "https://x.test/g1.html",
    ]
    assert {item.source for item in ranked} == {
        "site-search",
        "category",
        "sitemap",
    }


def test_structured_candidates_do_not_depend_on_bfs_depth():
    candidate = Candidate(
        "https://x.test/archive/2000/deep.html",
        "sitemap",
        score=55,
    )
    base = CrawlResult(
        pages=[CrawledPage("https://x.test/", "<html>首页</html>")]
    )

    assert candidate.url not in {page.url for page in base.pages}
    assert candidate.source == "sitemap"


class _ClientContext:
    def __init__(self):
        self.client = object()
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self.client

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


def _run(coro):
    return asyncio.run(coro)


def _install_engine_fakes(
    monkeypatch,
    *,
    batches: dict[str, list[Candidate] | BaseException] | None = None,
    fetch_html: Callable[[str], str | None] | None = None,
):
    import discovery.engine as engine

    batches = batches or {}
    context = _ClientContext()
    provider_records: list[tuple[str, object, object]] = []
    fetcher_records: list[object] = []

    class FakeFetcher:
        def __init__(self, client, budget, stats):
            assert client is context.client
            self.budget = budget
            self.stats = stats
            self.urls: list[str] = []
            fetcher_records.append(self)

        async def fetch_html(self, url: str):
            assert context.entered and not context.exited
            if not self.budget.reserve_html():
                self.stats.partial = True
                return None
            self.urls.append(url)
            if fetch_html is None:
                return f"<main>{url}</main>"
            return fetch_html(url)

    def provider_type(source):
        class FakeProvider:
            def __init__(self, client, budget, stats, policy, *args, **kwargs):
                assert client is context.client
                self.budget = budget
                self.stats = stats
                self.policy = policy
                self.args = args
                self.kwargs = kwargs
                provider_records.append((source, self, policy))

            async def discover(self, keywords):
                assert context.entered and not context.exited
                self.stats.sources_tried.add(source)
                batch = batches.get(source, [])
                if isinstance(batch, BaseException):
                    raise batch
                if batch:
                    self.stats.sources_succeeded.add(source)
                return list(batch)

        FakeProvider.source = source
        return FakeProvider

    monkeypatch.setattr(engine, "make_client", lambda: context)
    monkeypatch.setattr(engine, "DiscoveryFetcher", FakeFetcher)
    monkeypatch.setattr(
        engine, "SitemapProvider", provider_type("sitemap")
    )
    monkeypatch.setattr(engine, "FeedProvider", provider_type("feed"))
    monkeypatch.setattr(
        engine, "CategoryProvider", provider_type("category")
    )
    monkeypatch.setattr(
        engine, "SearchProvider", provider_type("site-search")
    )
    monkeypatch.setattr(
        engine,
        "FreeCmsApiProvider",
        provider_type("site-search-api"),
    )
    monkeypatch.setattr(
        engine, "YunnanCmsProvider", provider_type("yunnan-search")
    )
    return context, provider_records, fetcher_records


def test_discover_pages_isolates_provider_failure_and_fetches_other_candidate(
    monkeypatch,
):
    candidate = Candidate(
        "https://x.test/archive/deep/article.html",
        "sitemap",
        score=55,
    )
    context, _providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={
            "feed": RuntimeError("secret provider detail"),
            "sitemap": [candidate],
        },
    )

    result = _run(
        discover_pages(
            "https://x.test/",
            ["BODY-ONLY-8472"],
            CrawlResult(
                pages=[
                    CrawledPage(
                        "https://x.test/",
                        "<html><body>首页</body></html>",
                    )
                ]
            ),
            depth=1,
            render_mode="off",
        )
    )

    assert isinstance(result, DiscoveryRun)
    assert [page.url for page in result.pages] == [candidate.url]
    assert result.stats.candidates_found == 1
    assert result.stats.candidates_fetched == 1
    assert "sitemap" in result.stats.sources_succeeded
    assert result.stats.warnings == ["feed: RuntimeError"]
    assert "secret provider detail" not in result.stats.warnings[0]
    assert fetchers[0].urls == [candidate.url]
    assert context.exited is True


def test_discover_pages_propagates_provider_cancellation(monkeypatch):
    _install_engine_fakes(
        monkeypatch,
        batches={"feed": asyncio.CancelledError()},
    )

    with pytest.raises(asyncio.CancelledError):
        _run(
            discover_pages(
                "https://x.test/",
                [],
                CrawlResult(
                    pages=[
                        CrawledPage("https://x.test/", "<html></html>")
                    ]
                ),
                depth=3,
                render_mode="off",
            )
        )


def test_generic_discovery_extends_policy_and_deduplicates_specs_categories(
    monkeypatch,
):
    import discovery.engine as engine

    context, provider_records, _fetchers = _install_engine_fakes(monkeypatch)
    duplicate_spec = SearchSpec(
        "site-search",
        "https://search.portal.test/find",
        "q",
        "page",
    )
    duplicate_category = "https://www.portal.test/news/"
    monkeypatch.setattr(
        engine,
        "detect_search_specs",
        lambda homepage, start_url: [duplicate_spec, duplicate_spec],
    )
    monkeypatch.setattr(
        engine,
        "detect_feed_urls",
        lambda homepage, start_url: ["https://feeds.portal.test/rss.xml"],
    )
    monkeypatch.setattr(
        engine,
        "detect_category_urls",
        lambda homepage, start_url, policy: [
            duplicate_category,
            duplicate_category,
        ],
    )

    result = _run(
        discover_pages(
            "https://www.portal.test/start",
            ["alpha"],
            CrawlResult(
                pages=[
                    CrawledPage(
                        "https://www.portal.test/start",
                        "<html>generic</html>",
                    )
                ]
            ),
            depth=2,
            render_mode="auto",
        )
    )

    assert result.stats.profile == "generic"
    by_source = {source: provider for source, provider, _ in provider_records}
    search_provider = by_source["site-search"]
    category_provider = by_source["category"]
    feed_provider = by_source["feed"]
    assert search_provider.args == ([duplicate_spec],)
    assert category_provider.args == ([duplicate_category],)
    assert category_provider.kwargs["render_mode"] == "auto"
    assert feed_provider.args == (["https://feeds.portal.test/rss.xml"],)
    assert search_provider.policy.allowed_hosts >= {
        "www.portal.test",
        "search.portal.test",
        "feeds.portal.test",
    }
    assert context.exited is True


def test_redirected_homepage_url_drives_all_site_discovery(monkeypatch):
    actual_home = "https://www.new.test/sub/"
    fresh = Candidate(
        "https://www.new.test/sub/article.html",
        "sitemap",
        score=90,
    )
    context, provider_records, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={
            "sitemap": [
                Candidate(actual_home, "sitemap", score=100),
                fresh,
            ]
        },
    )
    homepage = """
    <html>
      <head>
        <link rel="alternate" type="application/rss+xml" href="feed.xml">
      </head>
      <body>
        <form method="get" action="search">
          <input name="q"><button>搜索</button>
        </form>
        <a href="news/">新闻</a>
      </body>
    </html>
    """

    result = _run(
        discover_pages(
            "https://old.test/original",
            ["alpha"],
            CrawlResult(pages=[CrawledPage(actual_home, homepage)]),
            depth=1,
            render_mode="off",
        )
    )

    by_source = {source: provider for source, provider, _ in provider_records}
    assert by_source["sitemap"].args == ("https://www.new.test",)
    assert by_source["feed"].args == (
        ["https://www.new.test/sub/feed.xml"],
    )
    assert by_source["category"].args == (
        ["https://www.new.test/sub/news"],
    )
    assert [spec.url for spec in by_source["site-search"].args[0]] == [
        "https://www.new.test/sub/search"
    ]
    assert all(
        policy.root_host == "www.new.test"
        and policy.allows("www.new.test")
        and not policy.allows("old.test")
        for _source, _provider, policy in provider_records
    )
    assert fetchers[0].urls == [fresh.url]
    assert [page.url for page in result.pages] == [fresh.url]
    assert context.exited is True


def test_freecms_uses_api_provider_and_keeps_category_fallback(monkeypatch):
    import discovery.engine as engine

    _context, provider_records, _fetchers = _install_engine_fakes(monkeypatch)
    monkeypatch.setattr(engine, "detect_search_specs", lambda *_: [])
    monkeypatch.setattr(engine, "detect_feed_urls", lambda *_: [])
    monkeypatch.setattr(engine, "detect_category_urls", lambda *_: [])

    result = _run(
        discover_pages(
            "https://www.zycg.gov.cn/",
            ["alpha"],
            CrawlResult(
                pages=[
                    CrawledPage(
                        "https://www.zycg.gov.cn/",
                        "<html>FreeCMS</html>",
                    )
                ]
            ),
            depth=1,
            render_mode="off",
        )
    )

    sources = [source for source, _provider, _policy in provider_records]
    assert "site-search-api" in sources
    assert "category" in sources
    assert "site-search" not in sources
    assert result.stats.profile == "freecms"


def test_yunnan_adapter_uses_yunnan_provider(monkeypatch):
    import discovery.engine as engine

    _context, provider_records, _fetchers = _install_engine_fakes(monkeypatch)
    monkeypatch.setattr(engine, "detect_search_specs", lambda *_: [])
    monkeypatch.setattr(engine, "detect_feed_urls", lambda *_: [])
    monkeypatch.setattr(engine, "detect_category_urls", lambda *_: [])

    result = _run(
        discover_pages(
            "https://dct.yn.gov.cn/",
            ["alpha"],
            CrawlResult(
                pages=[
                    CrawledPage(
                        "https://dct.yn.gov.cn/",
                        '<script src="/searchN.aspx"></script>',
                    )
                ]
            ),
            depth=1,
            render_mode="off",
        )
    )

    sources = [source for source, _provider, _policy in provider_records]
    assert "yunnan-search" in sources
    assert "site-search" not in sources
    assert result.stats.profile == "yunnan-cms"


def test_discover_pages_excludes_visited_normalized_urls(monkeypatch):
    visited_variant = Candidate(
        "https://x.test/a/?b=2&a=1#result",
        "sitemap",
        score=90,
    )
    fresh = Candidate("https://x.test/b", "sitemap", score=55)
    _context, _providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={"sitemap": [visited_variant, fresh]},
    )

    result = _run(
        discover_pages(
            "https://x.test/",
            ["alpha"],
            CrawlResult(
                pages=[
                    CrawledPage(
                        "https://x.test/a?a=1&b=2&utm_source=old",
                        "<html>visited</html>",
                    )
                ]
            ),
            depth=1,
            render_mode="off",
        )
    )

    assert [page.url for page in result.pages] == [fresh.url]
    assert result.stats.candidates_found == 1
    assert fetchers[0].urls == [fresh.url]


def test_skip_urls_are_not_fetched_or_charged_to_html_budget(monkeypatch):
    skipped = Candidate(
        "https://x.test/a/?utm_source=provider",
        "sitemap",
        score=90,
    )
    fresh = Candidate("https://x.test/b", "sitemap", score=55)
    _context, _providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={"sitemap": [skipped, fresh]},
    )

    result = _run(
        discover_pages(
            "https://x.test/",
            ["alpha"],
            CrawlResult(
                pages=[
                    CrawledPage(
                        "https://x.test/",
                        "<html>home</html>",
                    )
                ]
            ),
            depth=1,
            render_mode="off",
            skip_urls={
                "https://x.test/a?utm_campaign=legacy#seen",
            },
        )
    )

    assert [page.url for page in result.pages] == [fresh.url]
    assert result.stats.candidates_found == 1
    assert fetchers[0].urls == [fresh.url]
    assert fetchers[0].budget.used_html_pages == 2


def test_live_budget_after_providers_controls_initial_and_expanded_fetches(
    monkeypatch,
):
    import discovery.engine as engine

    candidates = [
        Candidate(
            f"https://x.test/{index}.html",
            "sitemap",
            score=100 - index,
        )
        for index in range(8)
    ]
    _context, _providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={"sitemap": candidates},
    )

    class ProviderBudgetConsumer:
        source = "feed"

        def __init__(self, client, budget, stats, policy, *args, **kwargs):
            self.budget = budget

        async def discover(self, keywords):
            for _ in range(3):
                assert self.budget.reserve_html()
            return []

    monkeypatch.setattr(engine, "FeedProvider", ProviderBudgetConsumer)
    monkeypatch.setattr(
        engine,
        "BudgetManager",
        lambda **kwargs: __import__(
            "discovery.models", fromlist=["BudgetManager"]
        ).BudgetManager(initial_pages=5, max_pages=8, **kwargs),
    )

    result = _run(
        discover_pages(
            "https://x.test/",
            ["alpha"],
            CrawlResult(
                pages=[CrawledPage("https://x.test/", "<html></html>")]
            ),
            depth=1,
            render_mode="off",
        )
    )

    assert result.stats.budget_expanded is True
    assert result.stats.candidates_found == 8
    assert result.stats.candidates_fetched == 4
    assert fetchers[0].budget.used_html_pages == 8
    assert len(fetchers[0].urls) == 4
    assert result.stats.partial is True


def test_budget_does_not_expand_for_only_low_value_remaining(monkeypatch):
    import discovery.engine as engine

    candidates = [
        Candidate(f"https://x.test/{index}", "category", score=54)
        for index in range(3)
    ]
    _context, _providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={"category": candidates},
    )
    monkeypatch.setattr(
        engine,
        "BudgetManager",
        lambda **kwargs: __import__(
            "discovery.models", fromlist=["BudgetManager"]
        ).BudgetManager(initial_pages=2, max_pages=5, **kwargs),
    )

    result = _run(
        discover_pages(
            "https://x.test/",
            ["alpha"],
            CrawlResult(
                pages=[CrawledPage("https://x.test/", "<html></html>")]
            ),
            depth=1,
            render_mode="off",
        )
    )

    assert len(fetchers[0].urls) == 1
    assert result.stats.budget_expanded is False
    assert result.stats.partial is True


def test_explicit_zero_started_at_is_preserved_and_prevents_requests(
    monkeypatch,
):
    candidate = Candidate("https://x.test/a", "sitemap", score=100)
    _context, _providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={"sitemap": [candidate]},
    )

    result = _run(
        discover_pages(
            "https://x.test/",
            ["alpha"],
            CrawlResult(
                pages=[CrawledPage("https://x.test/", "<html></html>")]
            ),
            depth=1,
            render_mode="off",
            timeout_seconds=1,
            started_at=0,
        )
    )

    assert fetchers[0].budget.started_at == 0
    assert fetchers[0].urls == []
    assert result.pages == []
    assert result.stats.partial is True
    assert result.stats.elapsed_ms > 0


def test_empty_base_and_invalid_start_url_are_safe(monkeypatch):
    import discovery.engine as engine

    _context, provider_records, _fetchers = _install_engine_fakes(monkeypatch)
    monkeypatch.setattr(engine, "detect_search_specs", lambda *_: [])
    monkeypatch.setattr(engine, "detect_feed_urls", lambda *_: [])
    monkeypatch.setattr(engine, "detect_category_urls", lambda *_: [])

    result = _run(
        discover_pages(
            "file:///tmp/page",
            [],
            CrawlResult(),
            depth=999,
            render_mode="off",
        )
    )

    assert result.pages == []
    assert result.stats.profile == "generic"
    sitemap_provider = next(
        provider
        for source, provider, _policy in provider_records
        if source == "sitemap"
    )
    assert sitemap_provider.args == ("",)


def test_origin_rejects_credentials_even_when_url_is_http(monkeypatch):
    import discovery.engine as engine

    _context, provider_records, _fetchers = _install_engine_fakes(monkeypatch)
    monkeypatch.setattr(engine, "detect_search_specs", lambda *_: [])
    monkeypatch.setattr(engine, "detect_feed_urls", lambda *_: [])
    monkeypatch.setattr(engine, "detect_category_urls", lambda *_: [])

    _run(
        discover_pages(
            "https://user:secret@x.test/start",
            [],
            CrawlResult(),
            depth=1,
            render_mode="off",
        )
    )

    sitemap_provider = next(
        provider
        for source, provider, _policy in provider_records
        if source == "sitemap"
    )
    assert sitemap_provider.args == ("",)


def test_fetch_failure_is_counted_as_found_not_fetched(monkeypatch):
    candidate = Candidate("https://x.test/a", "sitemap", score=100)

    def fail_fetch(url):
        return None

    _context, _providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={"sitemap": [candidate]},
        fetch_html=fail_fetch,
    )

    result = _run(
        discover_pages(
            "https://x.test/",
            ["alpha"],
            CrawlResult(),
            depth=1,
            render_mode="off",
        )
    )

    assert fetchers[0].urls == [candidate.url]
    assert result.stats.candidates_found == 1
    assert result.stats.candidates_fetched == 0


def test_elapsed_uses_supplied_start_time(monkeypatch):
    _install_engine_fakes(monkeypatch)
    started_at = time.monotonic() - 0.05

    result = _run(
        discover_pages(
            "https://x.test/",
            [],
            CrawlResult(),
            depth=1,
            render_mode="off",
            timeout_seconds=1,
            started_at=started_at,
        )
    )

    assert result.stats.elapsed_ms >= 40
