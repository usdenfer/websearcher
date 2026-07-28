from discovery.models import (
    BudgetManager,
    Candidate,
    DiscoveryStats,
    DomainPolicy,
    SearchSpec,
)


def test_candidate_identity_is_url_across_sources():
    search_candidate = Candidate(
        url="https://example.com/news/1",
        source="site_search",
        keyword="alpha",
        score=10,
    )
    sitemap_candidate = Candidate(
        url="https://example.com/news/1",
        source="sitemap",
        keyword="beta",
        score=80,
    )

    assert search_candidate == sitemap_candidate
    assert len({search_candidate, sitemap_candidate}) == 1


def test_domain_policy_allows_only_explicit_non_excluded_hosts():
    policy = DomainPolicy(
        root_host="example.com",
        allowed_hosts=frozenset({"www.example.com", "search.example.com"}),
        excluded_hosts=frozenset({"search.example.com"}),
        allow_related_hosts=False,
    )

    assert policy.allows("www.example.com")
    assert not policy.allows("search.example.com")
    assert not policy.allows("example.com")
    assert not policy.allows("news.example.com")


def test_budget_manager_stops_at_initial_limit_and_expands_for_high_value_only():
    budget = BudgetManager(initial_pages=60, max_pages=120, timeout_seconds=120)

    assert all(budget.reserve_html() for _ in range(60))
    assert budget.used_html_pages == 60
    assert not budget.reserve_html()
    assert not budget.expand(high_value_remaining=0)
    assert budget.page_limit == 60
    assert budget.expand(high_value_remaining=1)
    assert budget.page_limit == 120
    assert budget.reserve_html()
    assert budget.used_html_pages == 61


def test_budget_manager_tracks_provider_limits_by_source():
    budget = BudgetManager()

    assert budget.reserve_provider("bing", limit=2)
    assert budget.reserve_provider("bing", limit=2)
    assert not budget.reserve_provider("bing", limit=2)
    assert budget.reserve_provider("google", limit=1)
    assert not budget.reserve_provider("google", limit=1)
    assert budget.provider_requests == {"bing": 2, "google": 1}


def test_budget_manager_expiration_uses_monotonic(monkeypatch):
    clock = iter((219.9, 220.0))
    monkeypatch.setattr("discovery.models.time.monotonic", lambda: next(clock))
    budget = BudgetManager(timeout_seconds=120, started_at=100.0)

    assert not budget.expired()
    assert budget.expired()


def test_discovery_stats_as_dict_exposes_stable_api_shape(monkeypatch):
    monkeypatch.setattr("discovery.models.time.monotonic", lambda: 12.345)
    stats = DiscoveryStats(
        profile="general",
        sources_tried={"sitemap", "site_search"},
        sources_succeeded={"site_search", "sitemap"},
        candidates_found=14,
        candidates_fetched=8,
        rendered_pages=2,
        budget_expanded=True,
        partial=True,
        started_at=10.0,
        warnings=["provider unavailable"],
    )

    assert stats.as_dict() == {
        "profile": "general",
        "sourcesTried": ["site_search", "sitemap"],
        "sourcesSucceeded": ["site_search", "sitemap"],
        "candidatesFound": 14,
        "candidatesFetched": 8,
        "renderedPages": 2,
        "budgetExpanded": True,
        "partial": True,
        "elapsedMs": 2345,
        "warnings": ["provider unavailable"],
    }


def test_search_spec_params_merge_fixed_keyword_and_first_page():
    spec = SearchSpec(
        source="internal_search",
        url="https://example.com/search",
        query_param="query",
        page_param="p",
        fixed_params=(("type", "article"), ("lang", "en")),
    )

    assert spec.params_for("climate", page=1) == {
        "type": "article",
        "lang": "en",
        "query": "climate",
        "p": 1,
    }


def test_search_spec_omits_page_when_page_param_is_absent():
    spec = SearchSpec(
        source="internal_search",
        url="https://example.com/search",
        query_param="q",
        fixed_params=(("scope", "all"),),
    )

    assert spec.params_for("energy", page=3) == {
        "scope": "all",
        "q": "energy",
    }
