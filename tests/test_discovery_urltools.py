import pytest

import discovery.urltools as urltools
from discovery.models import DomainPolicy
from discovery.urltools import (
    canonical_url,
    extend_policy_with_declared_urls,
    is_html_candidate,
    normalize_candidate_url,
    registrable_domain,
    url_allowed,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTP://ExAmPle.COM.:80/a", ("example.com", 80)),
        ("https://例子.测试:443/a", ("xn--fsqu00a.xn--0zwm56d", 443)),
        ("http://[2001:0db8::1]:80/", ("2001:db8::1", 80)),
        ("https://example.com:8443/", ("example.com", 8443)),
        ("https://user:pass@example.com/", None),
    ],
)
def test_canonical_authority(url, expected):
    assert hasattr(urltools, "canonical_authority")
    assert urltools.canonical_authority(url) == expected


def test_normalize_drops_tracking_but_keeps_semantic_parameters():
    url = (
        "  HTTPS://WWW.EXAMPLE.COM/a/?Utm_Source=x&id=7&page=2"
        "&SPM=ad&empty=#section  "
    )
    assert normalize_candidate_url(url) == (
        "https://www.example.com/a?empty=&id=7&page=2"
    )


def test_normalize_sorts_query_for_stable_deduplication():
    assert normalize_candidate_url("https://x.test/a?id=1&b=2") == (
        "https://x.test/a?b=2&id=1"
    )
    assert normalize_candidate_url("https://x.test") == "https://x.test/"
    assert normalize_candidate_url("https://x.test/a///") == "https://x.test/a"


def test_normalize_preserves_order_of_repeated_parameter_values():
    assert normalize_candidate_url(
        "https://x.test/a?x=1&step=b&step=a"
    ) == "https://x.test/a?step=b&step=a&x=1"


def test_normalize_lowercases_host_without_changing_userinfo():
    assert normalize_candidate_url(
        "HTTPS://User:PaSS@EXAMPLE.COM:8443/a"
    ) == "https://User:PaSS@example.com:8443/a"
    assert normalize_candidate_url(
        "HTTPS://[2001:DB8::1]:8443/a"
    ) == "https://[2001:db8::1]:8443/a"


def test_url_allowed_uses_explicit_content_hosts():
    policy = DomainPolicy(
        "www.example.test",
        frozenset({"www.example.test", "content.example.test"}),
    )
    assert url_allowed("https://content.example.test/2024/x/", policy)
    assert not url_allowed("https://ads.example.test/x", policy)
    assert not url_allowed("javascript:void(0)", policy)


def test_binary_and_auth_pages_are_not_html_candidates():
    assert not is_html_candidate("https://x.test/a.PDF?id=7")
    assert not is_html_candidate("https://x.test/login")
    assert not is_html_candidate("https://x.test/news/user/profile")
    assert not is_html_candidate("https://x.test/register/account")
    assert is_html_candidate("https://x.test/news/123.shtml?id=7")


def test_html_candidate_requires_http_host():
    assert not is_html_candidate("/relative/article.html")
    assert not is_html_candidate("ftp://x.test/article.html")
    assert not is_html_candidate("https:///article.html")


def test_malformed_ipv6_authority_is_rejected_without_raising():
    malformed = "https://[::1"
    assert normalize_candidate_url(malformed) == ""
    assert not is_html_candidate(malformed)
    policy = DomainPolicy("x.test", frozenset({"x.test"}))
    assert not url_allowed(malformed, policy)
    extended = extend_policy_with_declared_urls(policy, [malformed])
    assert extended.allowed_hosts == policy.allowed_hosts


def test_canonical_is_used_only_inside_domain_policy():
    policy = DomainPolicy("x.test", frozenset({"x.test"}))
    assert canonical_url(
        '<link rel="canonical" href="/clean.html?id=7&utm_medium=x">',
        "https://x.test/a?utm_source=x",
        policy,
    ) == "https://x.test/clean.html?id=7"
    assert canonical_url(
        '<link rel="canonical" href="https://outside.test/a">',
        "https://x.test/original.html",
        policy,
    ) == "https://x.test/original.html"


def test_canonical_rel_is_case_insensitive_and_malformed_target_falls_back():
    policy = DomainPolicy("x.test", frozenset({"x.test"}))
    assert canonical_url(
        '<link rel="alternate Canonical stylesheet" href="/clean.html">',
        "https://x.test/original.html",
        policy,
    ) == "https://x.test/clean.html"
    assert canonical_url(
        '<link rel="Canonical" href="https://[::1">',
        "https://x.test/original.html?utm_source=x",
        policy,
    ) == "https://x.test/original.html"


def test_declared_search_subdomain_can_extend_generic_policy():
    policy = DomainPolicy(
        "www.example.co.uk",
        frozenset({"www.example.co.uk"}),
        frozenset({"blocked.example.co.uk"}),
    )
    extended = extend_policy_with_declared_urls(
        policy,
        [
            "https://search.example.co.uk/find?q=x",
            "https://attacker.co.uk/fake",
        ],
    )
    assert extended.allows("search.example.co.uk")
    assert not extended.allows("attacker.co.uk")
    assert extended.excluded_hosts == policy.excluded_hosts
    assert extended.allow_related_hosts is True
    assert url_allowed("https://content.example.co.uk/article.html", extended)
    assert not url_allowed("https://ads.example.co.uk/banner.html", extended)
    assert not url_allowed("https://blocked.example.co.uk/a", extended)
    assert registrable_domain("WWW.Example.Co.UK.") == "example.co.uk"


def test_registrable_domain_supports_known_multipart_suffixes():
    assert registrable_domain("news.example.gov.cn") == "example.gov.cn"
    assert registrable_domain("a.b.example.com.au") == "example.com.au"
    assert registrable_domain("www.example.com") == "example.com"
