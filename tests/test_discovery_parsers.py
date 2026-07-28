from pathlib import Path

from discovery.models import DomainPolicy
from discovery.parsers import (
    detect_category_urls,
    detect_feed_urls,
    detect_search_specs,
    parse_feed,
    parse_pagination,
    parse_result_candidates,
    parse_sitemap,
    parse_sitemap_index,
)

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
POLICY = DomainPolicy("example.test", frozenset({"example.test"}))


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_detects_get_search_forms_script_feed_and_category_urls():
    html = fixture("generic_search.html")

    specs = detect_search_specs(html, "https://example.test/")

    assert [(spec.url, spec.query_param) for spec in specs] == [
        ("https://example.test/search", "keyword"),
        ("https://example.test/data-search", "searchContent"),
        ("https://example.test/find", "query"),
    ]
    assert all(spec.url != "https://example.test/ignored" for spec in specs)
    assert detect_feed_urls(html, "https://example.test/") == [
        "https://example.test/feed.xml"
    ]
    assert detect_category_urls(
        html,
        "https://example.test/",
        POLICY,
    ) == ["https://example.test/news/index.html"]


def test_search_detection_is_case_insensitive_and_requires_search_semantics():
    html = """
    <form action="/upper"><input name="KEYWORDS"><button>SEARCH</button></form>
    <form action="/submit"><input name="q"><button>提交</button></form>
    <script>window.location = '/QUERY?SearchContent=' + value;</script>
    """

    specs = detect_search_specs(html, "https://example.test/")

    assert [(spec.url, spec.query_param) for spec in specs] == [
        ("https://example.test/upper", "KEYWORDS"),
        ("https://example.test/QUERY", "SearchContent"),
    ]


def test_detects_declarative_data_action_without_visible_search_text():
    specs = detect_search_specs(
        '<div data-action="/lookup"><input name="q"></div>',
        "https://example.test/",
    )

    assert [(spec.url, spec.query_param) for spec in specs] == [
        ("https://example.test/lookup", "q")
    ]


def test_parses_result_candidates_and_pagination():
    html = fixture("generic_results.html")

    candidates = parse_result_candidates(
        html,
        "https://example.test/search?keyword=alpha",
        POLICY,
        source="site-search",
        keyword="正文标记",
    )

    assert [(item.url, item.title_hint, item.score) for item in candidates] == [
        ("https://example.test/news/1.html", "普通标题", 70),
        ("https://example.test/news/2.html", "正文标记对应页面", 100),
    ]
    assert all(item.source == "site-search" for item in candidates)
    assert all(item.keyword == "正文标记" for item in candidates)
    assert parse_pagination(
        html,
        "https://example.test/search?keyword=alpha",
        POLICY,
    ) == [
        "https://example.test/search?keyword=alpha&page=2",
        "https://example.test/search?keyword=alpha&page=3",
    ]


def test_result_parser_falls_back_to_all_anchors_without_result_containers():
    candidates = parse_result_candidates(
        '<a href="/article/1.html">Alpha</a><a href="/a.pdf">PDF</a>',
        "https://example.test/",
        POLICY,
        source="category",
        keyword="alpha",
    )

    assert [(item.url, item.score) for item in candidates] == [
        ("https://example.test/article/1.html", 100)
    ]


def test_parses_sitemap_feed_and_sitemap_index():
    assert parse_sitemap(fixture("sitemap.xml"), POLICY) == [
        "https://example.test/news/1.html",
        "https://example.test/news/2.html",
    ]
    feed = parse_feed(fixture("feed.xml"), POLICY)
    assert [(item.url, item.title_hint, item.source, item.score) for item in feed] == [
        ("https://example.test/news/3.html", "条目一", "feed", 65)
    ]
    index = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.test/sitemap-news.xml</loc></sitemap>
      <sitemap><loc>https://example.test/sitemap-news.xml#duplicate</loc></sitemap>
      <sitemap><loc>http://[invalid</loc></sitemap>
    </sitemapindex>"""
    assert parse_sitemap_index(index, POLICY) == [
        "https://example.test/sitemap-news.xml"
    ]
    assert parse_sitemap_index(fixture("sitemap.xml"), POLICY) == []


def test_parses_atom_link_href_and_skips_malformed_urls():
    atom = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Atom 条目</title>
        <link href="https://example.test/news/atom.html"/>
      </entry>
      <entry><title>坏链接</title><link href="http://[invalid"/></entry>
    </feed>"""

    feed = parse_feed(atom, POLICY)

    assert [(item.url, item.title_hint) for item in feed] == [
        ("https://example.test/news/atom.html", "Atom 条目")
    ]
