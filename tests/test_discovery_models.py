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
        published_date="2026-07-20",
        source_evidence=("site-search-api",),
    )
    sitemap_candidate = Candidate(
        url="https://example.com/news/1",
        source="sitemap",
        keyword="beta",
        score=80,
        published_date=None,
        source_evidence=("freecms-recent",),
    )

    assert search_candidate == sitemap_candidate
    assert len({search_candidate, sitemap_candidate}) == 1
    assert search_candidate.published_date == "2026-07-20"
    assert search_candidate.source_evidence == ("site-search-api",)


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


def test_budget_exposes_non_negative_remaining_deadline(monkeypatch):
    monkeypatch.setattr("discovery.models.time.monotonic", lambda: 130.0)
    budget = BudgetManager(timeout_seconds=120, started_at=100.0)

    assert budget.deadline == 220.0
    assert budget.remaining_seconds() == 90.0

    monkeypatch.setattr("discovery.models.time.monotonic", lambda: 230.0)
    assert budget.remaining_seconds() == 0.0


def test_discovery_stats_defaults_to_generic_profile():
    stats = DiscoveryStats()

    assert stats.profile == "generic"
    assert stats.recent_window_days is None
    assert stats.weak_candidates == 0
    assert stats.unknown_date_candidates == 0
    assert stats.stop_reason is None


def test_discovery_stats_as_dict_exposes_stable_api_shape():
    stats = DiscoveryStats(
        profile="general",
        sources_tried={"sitemap", "site_search"},
        sources_succeeded={"site_search", "sitemap"},
        candidates_found=14,
        candidates_fetched=8,
        rendered_pages=2,
        budget_expanded=True,
        partial=True,
        elapsed_ms=2345,
        warnings=["provider unavailable"],
        recent_window_days=30,
        weak_candidates=4,
        unknown_date_candidates=3,
        stop_reason="date-boundary",
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
        "recentWindowDays": 30,
        "weakCandidates": 4,
        "unknownDateCandidates": 3,
        "stopReason": "date-boundary",
    }


def test_discovery_stats_note_stop_keeps_highest_priority_reason():
    ordered_reasons = (
        "date-boundary",
        "provider-page-limit",
        "channel-failure",
        "html-page-budget",
        "time-budget",
    )

    for higher_index, higher in enumerate(ordered_reasons[1:], start=1):
        for lower in ordered_reasons[:higher_index]:
            higher_first = DiscoveryStats()
            higher_first.note_stop(higher)
            higher_first.note_stop(lower)
            assert higher_first.stop_reason == higher

            lower_first = DiscoveryStats()
            lower_first.note_stop(lower)
            lower_first.note_stop(higher)
            assert lower_first.stop_reason == higher

    unknown = DiscoveryStats()
    unknown.note_stop("unknown-reason")
    assert unknown.stop_reason is None


def test_discovery_stats_unknown_stop_does_not_replace_known_reason():
    stats = DiscoveryStats()

    stats.note_stop("channel-failure")
    stats.note_stop("unknown-reason")

    assert stats.stop_reason == "channel-failure"


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
