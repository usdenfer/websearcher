from pathlib import Path

import pytest

from discovery.adapters import (
    FreeCmsAdapter,
    SiteAdapter,
    YunnanCmsAdapter,
    select_adapter,
)
from discovery.parsers import detect_search_specs, parse_result_candidates
from discovery.urltools import extend_policy_with_declared_urls

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_experimental_portal_uses_generic_adapter_across_declared_subdomains():
    homepage = fixture("portal_search_home.html")
    adapter = select_adapter("https://www.portal.test/", homepage)

    assert type(adapter) is SiteAdapter
    assert adapter.profile == "generic"
    assert adapter.search_specs() == []
    assert adapter.category_urls() == []

    specs = detect_search_specs(homepage, "https://www.portal.test/")
    assert [spec.url for spec in specs] == [
        "https://search.portal.test/"
    ]
    policy = extend_policy_with_declared_urls(
        adapter.domain_policy("https://www.portal.test/"),
        [spec.url for spec in specs],
    )
    candidates = parse_result_candidates(
        fixture("portal_search_results.html"),
        specs[0].url,
        policy,
        source="site-search",
        keyword="正文关键字",
    )

    assert {candidate.url for candidate in candidates} == {
        "https://www.portal.test/news/202607/1.shtml",
        "https://content.portal.test/2026/example",
    }


def test_generic_domain_policy_contains_only_safely_parsed_root_host():
    adapter = SiteAdapter()

    policy = adapter.domain_policy("HTTPS://WWW.Example.TEST:8443/start")

    assert policy.root_host == "www.example.test"
    assert policy.allowed_hosts == frozenset({"www.example.test"})
    assert policy.excluded_hosts == frozenset()
    assert policy.allow_related_hosts is False


@pytest.mark.parametrize(
    ("adapter_type", "start_url", "expected_origin"),
    [
        (FreeCmsAdapter, "http://EXAMPLE.test/path", "http://example.test"),
        (
            FreeCmsAdapter,
            "https://EXAMPLE.test:8443/path",
            "https://example.test:8443",
        ),
        (
            YunnanCmsAdapter,
            "https://[2001:DB8::1]:9443/path",
            "https://[2001:db8::1]:9443",
        ),
    ],
)
def test_specialized_adapter_origin_is_rebuilt_from_safe_authority(
    adapter_type,
    start_url,
    expected_origin,
):
    assert adapter_type(start_url).origin == expected_origin


@pytest.mark.parametrize(
    "start_url",
    [
        "file://example.test/content",
        "https://user:pass@example.test/",
        "https://example.test:invalid/",
        "https://[::1",
        "https:///missing-host",
    ],
)
@pytest.mark.parametrize("adapter_type", [FreeCmsAdapter, YunnanCmsAdapter])
def test_specialized_adapters_reject_unsafe_or_invalid_origins(
    adapter_type,
    start_url,
):
    with pytest.raises(ValueError, match="有效的 HTTP"):
        adapter_type(start_url)


@pytest.mark.parametrize("start_url", ["", "not a url", "https://[::1"])
def test_adapter_selection_and_policy_tolerate_invalid_start_urls(start_url):
    adapter = select_adapter(start_url, "")

    assert type(adapter) is SiteAdapter
    policy = adapter.domain_policy(start_url)
    assert policy.root_host == ""
    assert policy.allowed_hosts == frozenset()


def test_freecms_adapter_exposes_api_search_and_fallback_categories():
    adapter = select_adapter(
        "https://www.zycg.gov.cn/path",
        fixture("freecms_home.html"),
    )

    assert isinstance(adapter, FreeCmsAdapter)
    assert adapter.profile == "freecms"
    assert adapter.origin == "https://www.zycg.gov.cn"
    assert adapter.domain_policy("https://www.zycg.gov.cn/path").allowed_hosts == (
        frozenset({"www.zycg.gov.cn"})
    )
    spec = adapter.search_specs()[0]
    assert spec.source == "site-search-api"
    assert spec.url == (
        "https://www.zycg.gov.cn/freecms/rest/v1/notice/searchAll.do"
    )
    assert spec.query_param == "title"
    assert spec.page_param == "currPage"
    assert spec.fixed_params == (("pageSize", "10"),)
    assert spec.params_for("正文关键字", page=3) == {
        "pageSize": "10",
        "title": "正文关键字",
        "currPage": 3,
    }
    assert adapter.category_urls() == [
        "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/cggg/index.html",
        "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/zxgklanmu/index.html",
        "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/zdfg/index.html",
    ]


def test_freecms_is_selected_by_domain_or_homepage_marker():
    assert isinstance(
        select_adapter("https://sub.zycg.gov.cn/", ""),
        FreeCmsAdapter,
    )
    assert isinstance(
        select_adapter(
            "https://procurement.example.test/",
            fixture("freecms_search.html"),
        ),
        FreeCmsAdapter,
    )


def test_freecms_api_response_accepts_dict_rows_and_list_payload():
    adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")

    assert adapter.parse_api_response(
        '{"code": 0, "data": {"rows": [{"pageUrl": "/notice/1"}]}}'
    ) == (True, [{"pageUrl": "/notice/1"}], "")
    assert adapter.parse_api_response(
        '{"code": "200", "data": [{"pageUrl": "/notice/2"}]}'
    ) == (True, [{"pageUrl": "/notice/2"}], "")
    assert adapter.parse_api_response(
        '{"code": 0, "data": []}'
    ) == (True, [], "")
    assert adapter.parse_api_response(
        '{"code": 200, "data": {"rows": []}}'
    ) == (True, [], "")


@pytest.mark.parametrize(
    "body",
    [
        '{"code": 0}',
        '{"code": 0, "data": null}',
        '{"code": 0, "data": "unexpected"}',
        '{"code": 0, "data": {"rows": null}}',
        '{"code": 0, "data": {"rows": {"pageUrl": "/notice/1"}}}',
        '{"code": 0, "data": [1]}',
        '{"code": 0, "data": [{"pageUrl": "/notice/1"}, "bad-row"]}',
    ],
)
def test_freecms_success_code_rejects_malformed_payloads(body):
    ok, rows, warning = FreeCmsAdapter(
        "https://www.zycg.gov.cn/"
    ).parse_api_response(body)

    assert ok is False
    assert rows == []
    assert "数据结构无效" in warning


def test_freecms_business_failure_is_not_empty_success_and_preserves_message():
    adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")

    ok, rows, warning = adapter.parse_api_response(
        fixture("freecms_failure.json")
    )

    assert ok is False
    assert rows == []
    assert warning == "公告列表查询失败"


def test_freecms_invalid_json_is_reported_in_chinese():
    ok, rows, warning = FreeCmsAdapter(
        "https://www.zycg.gov.cn/"
    ).parse_api_response("{not-json")

    assert ok is False
    assert rows == []
    assert warning == "FreeCMS 搜索接口返回了无效 JSON"


def test_yunnan_adapter_remains_available_with_expected_search_parameters():
    adapter = select_adapter(
        "https://dct.yn.gov.cn/path",
        '<script src="/searchClassCount.aspx"></script>',
    )

    assert isinstance(adapter, YunnanCmsAdapter)
    assert adapter.profile == "yunnan-cms"
    assert adapter.origin == "https://dct.yn.gov.cn"
    spec = adapter.search_specs()[0]
    assert spec.source == "site-search"
    assert spec.url == "https://dct.yn.gov.cn/searchN.aspx"
    assert spec.query_param == "tags"
    assert spec.page_param == "page"
    assert spec.fixed_params == (("type", ""),)


def test_yunnan_adapter_is_selected_by_searchn_marker():
    adapter = select_adapter(
        "https://cms.example.test/",
        '<script src="/searchN.aspx"></script>',
    )

    assert isinstance(adapter, YunnanCmsAdapter)


@pytest.mark.parametrize(
    ("start_url", "homepage"),
    [
        (
            "https://www.zycg.gov.cn/",
            '<script src="/searchN.aspx"></script>',
        ),
        (
            "https://cms.example.test/",
            '<script src="/searchN.aspx"></script>'
            '<script>fetch("/searchAll.do")</script>',
        ),
    ],
)
def test_freecms_detection_has_priority_over_yunnan_markers(
    start_url,
    homepage,
):
    assert isinstance(
        select_adapter(start_url, homepage),
        FreeCmsAdapter,
    )


def test_yunnan_adapter_selected_by_searchgo_script_marker():
    """真实云南 CMS 首页不直接引用 searchN.aspx，搜索入口是
    HSearchGo()/MSearchGo()（跳转 Search.html?tags=），也要能识别。"""
    homepage = (
        '<input name="Htags" id="HKeyword" '
        "onkeydown=\"if(event.keyCode==13){HSearchGo();}\">"
        '<a class="search_btn" onclick="HSearchGo();">搜索</a>'
        '<input name="Mtags" id="MKeyword" '
        "onkeydown=\"if(event.keyCode==13){MSearchGo();}\">"
    )
    adapter = select_adapter("https://dct.yn.gov.cn/", homepage)
    assert isinstance(adapter, YunnanCmsAdapter)
    assert adapter.profile == "yunnan-cms"
    assert adapter.search_specs()[0].url == \
        "https://dct.yn.gov.cn/searchN.aspx"


def test_freecms_domain_policy_allows_related_content_hosts():
    from discovery.urltools import url_allowed

    adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
    policy = adapter.domain_policy("https://www.zycg.gov.cn/")

    assert policy.root_host == "www.zycg.gov.cn"
    assert policy.allowed_hosts == frozenset({"www.zycg.gov.cn"})
    assert policy.allow_related_hosts is True
    assert url_allowed(
        "http://mkt.zycg.gov.cn/mall-view/information/detail?noticeId=1",
        policy,
    )
    assert url_allowed(
        "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/ggxx/info/1.html",
        policy,
    )
