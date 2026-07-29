"""matcher.py 的单元测试：覆盖全部 kind、大小写、片段、去重。"""
from types import SimpleNamespace

from discovery.models import Candidate
from matcher import (SNIPPET_RADIUS, extract_main_text, extract_title,
                    looks_js_driven, make_snippet, match_body_crawl_result,
                    match_body_page, match_body_with_recall, match_page)

PAGE = "http://test.local/page"

HTML = """
<html><head>
<title>Home Page</title>
<meta name="description" content="Delta site overview">
<meta property="og:title" content="Delta OG title">
<meta name="viewport" content="width=device-width">
<script>var keywordInsideScript = "Alpha";</script>
<style>.alpha-class { color: red; }</style>
</head><body>
<p>Visible Alpha text in body.</p>
<a href="/sub1">Epsilon page</a>
<a href="https://other.example.com/zeta-path">outer</a>
<img src="/img/theta-chart.png" alt="Beta diagram" title="Iota info">
<nav aria-label="Gamma navigation">nav</nav>
<input placeholder="Zeta search box" value="Kappa value">
<!-- Alpha in comment -->
</body></html>
"""


def kinds(hits):
    return {h.kind for h in hits}


def test_text_hit():
    hits = match_page(HTML, PAGE, ["alpha"])
    text_hits = [h for h in hits if h.kind == "text"]
    assert len(text_hits) == 1  # script/style/comment/a 内文本不算 text
    assert "Visible Alpha text" in text_hits[0].snippet
    assert text_hits[0].href == PAGE + "#:~:text=alpha"
    assert text_hits[0].keyword == "alpha"


def test_all_kinds():
    hits = match_page(HTML, PAGE,
                      ["beta", "theta", "iota", "gamma", "epsilon",
                       "zeta", "kappa", "delta"])
    assert kinds(hits) == {"img-alt", "img-src", "title-attr", "aria-label",
                           "link-text", "link-href", "meta", "form"}


def test_img_alt_and_src_and_title():
    hits = match_page(HTML, PAGE, ["beta", "theta", "iota"])
    by_kind = {h.kind: h for h in hits}
    assert by_kind["img-alt"].snippet == "Beta diagram"
    assert by_kind["img-src"].snippet == "theta-chart.png"
    assert by_kind["title-attr"].snippet == "Iota info"
    assert all(h.href == PAGE for h in hits)


def test_link_kinds():
    hits = match_page(HTML, PAGE, ["epsilon", "zeta"])
    lt = [h for h in hits if h.kind == "link-text"][0]
    lh = [h for h in hits if h.kind == "link-href"][0]
    assert lt.snippet == "Epsilon page"
    # linkHref 解析为绝对地址，前端可直接点击
    assert lt.linkHref == "http://test.local/sub1"
    # 链接类命中的跳转定位直达目标页（正文页），而不是回到包含页
    assert lt.href == "http://test.local/sub1"
    assert lh.linkHref == "https://other.example.com/zeta-path"
    assert lh.href == "https://other.example.com/zeta-path"
    assert "zeta-path" in lh.snippet


def test_meta_hit_excludes_viewport():
    hits = match_page(HTML, PAGE, ["delta", "device-width"])
    meta_hits = [h for h in hits if h.kind == "meta"]
    assert len(meta_hits) == 2  # description + og:title，不含 viewport
    assert {h.snippet for h in meta_hits} == {"Delta site overview",
                                              "Delta OG title"}


def test_form_hits():
    hits = match_page(HTML, PAGE, ["zeta", "kappa"])
    form_hits = [h for h in hits if h.kind == "form"]
    assert {h.snippet for h in form_hits} == {"Zeta search box", "Kappa value"}


def test_case_insensitive_and_no_match():
    assert match_page(HTML, PAGE, ["ALPHA"]) != []
    assert match_page(HTML, PAGE, ["not-present-anywhere"]) == []


def test_snippet_radius():
    text = "x" * 200 + "Keyword" + "y" * 200
    snippet = make_snippet(text, "keyword")
    assert snippet.startswith("…") and snippet.endswith("…")
    assert snippet.index("Keyword") == SNIPPET_RADIUS + 1  # 省略号占 1 字符
    assert make_snippet("nothing here", "keyword") is None


def test_dedupe_same_kind_and_snippet():
    html = "<p>dup Alpha</p><p>dup Alpha</p>"
    hits = match_page(html, PAGE, ["alpha"])
    assert len(hits) == 1


def test_extract_title():
    assert extract_title(HTML) == "Home Page"
    assert extract_title("<html><body>no title</body></html>") == ""


def test_looks_js_driven_detects_ajax_lists():
    js_heavy = """<html><body><div id="classlist"></div>
    <script>$.ajax({url: "../../listajax.aspx?PageSize=10", success: function(r){
      $("#classlist").html(r);}});</script></body></html>"""
    assert looks_js_driven(js_heavy)


def test_looks_js_driven_static_site_false():
    static = """<html><body><ul><li><a href="a.html">文章</a></li></ul>
    <script>console.log("analytics");</script></body></html>"""
    assert not looks_js_driven(static)


def test_extract_main_text_removes_navigation_and_footer():
    html = """
    <html><body>
      <nav>导航伪命中词</nav>
      <main><article><p>真正正文随机标记 QX-7319</p></article></main>
      <footer>页脚伪命中词</footer>
    </body></html>
    """
    text = extract_main_text(html)
    assert "QX-7319" in text
    assert "导航伪命中词" not in text
    assert "页脚伪命中词" not in text


def test_match_body_page_ignores_title_url_and_search_metadata():
    html = """
    <html><head>
      <title>标题只有 TITLE-ONLY</title>
      <meta name="description" content="META-ONLY">
    </head><body>
      <nav><a href="/URL-ONLY">LINK-ONLY</a></nav>
      <article><p>正文唯一标记 BODY-ONLY-8472</p></article>
    </body></html>
    """
    hits = match_body_page(
        html, "https://x.test/URL-ONLY", [
            "TITLE-ONLY", "META-ONLY", "LINK-ONLY", "BODY-ONLY-8472",
        ])
    assert [hit.keyword for hit in hits] == ["BODY-ONLY-8472"]
    assert "正文唯一标记" in hits[0].snippet


def test_extract_main_text_removes_noise_classes_and_hidden_content():
    html = """
    <body>
      <div class="navbar">NAVBAR-NOISE</div>
      <div class="sidebar">SIDEBAR-NOISE</div>
      <div class="advert">ADVERT-NOISE</div>
      <div class="ad">AD-NOISE</div>
      <div class="login">LOGIN-NOISE</div>
      <div hidden>HIDDEN-NOISE</div>
      <div aria-hidden="true">ARIA-NOISE</div>
      <main>正文标记 MAIN-MARK-3107</main>
    </body>
    """
    text = extract_main_text(html)
    assert "MAIN-MARK-3107" in text
    assert all(noise not in text for noise in (
        "NAVBAR-NOISE", "SIDEBAR-NOISE", "ADVERT-NOISE", "AD-NOISE",
        "LOGIN-NOISE", "HIDDEN-NOISE", "ARIA-NOISE",
    ))


def test_extract_main_text_removes_aria_hidden_case_insensitively():
    html = """
    <main>
      <div aria-hidden="TRUE">UPPER-HIDDEN-4182</div>
      <div aria-hidden=" True ">MIXED-HIDDEN-5293</div>
      <p>可见正文 VISIBLE-MARK-6304</p>
    </main>
    """
    text = extract_main_text(html)
    assert "VISIBLE-MARK-6304" in text
    assert "UPPER-HIDDEN-4182" not in text
    assert "MIXED-HIDDEN-5293" not in text


def test_extract_main_text_uses_role_main():
    html = """
    <body>
      <section>外围标记 OUTSIDE-MARK</section>
      <section role="main">角色正文 ROLE-MAIN-5204</section>
    </body>
    """
    text = extract_main_text(html)
    assert text == "角色正文 ROLE-MAIN-5204"


def test_extract_main_text_does_not_choose_tiny_article_over_main_content():
    html = """
    <body>
      <article>短标签</article>
      <main>
      <section>完整正文中的标记 FULL-BODY-6421</section>
      </main>
    </body>
    """
    text = extract_main_text(html)
    assert "FULL-BODY-6421" in text


def test_extract_main_text_chooses_longer_main_over_long_article():
    article_text = "文章片段" * 25
    main_text = "完整正文" * 40 + " LONG-MAIN-7415"
    html = (
        f"<body><article>{article_text}</article>"
        f"<main>{main_text}</main></body>"
    )
    text = extract_main_text(html)
    assert "LONG-MAIN-7415" in text
    assert text == main_text


def test_match_body_page_strips_and_stably_deduplicates_keywords():
    hits = match_body_page(
        "<main>正文含有 Alpha 和 beta。</main>",
        PAGE,
        [" ", " Alpha ", "alpha", "", " beta ", "Alpha"],
    )
    assert [hit.keyword for hit in hits] == ["Alpha", "alpha", "beta"]
    assert all(hit.kind == "text" for hit in hits)
    assert hits[0].href == PAGE + "#:~:text=Alpha"
    assert hits[0].linkHref is None


def test_match_body_page_handles_missing_body_and_malformed_html():
    assert match_body_page("<title>ONLY-TITLE</title>", PAGE,
                           ["ONLY-TITLE"]) == []
    hits = match_body_page(
        "<main><p>未闭合正文 MALFORMED-7315",
        PAGE,
        ["MALFORMED-7315"],
    )
    assert [hit.keyword for hit in hits] == ["MALFORMED-7315"]


def test_match_body_crawl_result_aggregates_only_matching_pages():
    pages = [
        SimpleNamespace(
            url="https://x.test/one",
            html="<title>第一页</title><main>正文 FIRST-8426</main>",
        ),
        SimpleNamespace(
            url="https://x.test/two",
            html="<title>标题 SECOND-ONLY</title><main>没有匹配</main>",
        ),
        SimpleNamespace(
            url="https://x.test/three",
            html="<title>第三页</title><article>SECOND-9537 FIRST-8426</article>",
        ),
    ]
    results, total = match_body_crawl_result(
        pages, ["FIRST-8426", "SECOND-9537"])
    assert total == 3
    assert [item["pageUrl"] for item in results] == [
        "https://x.test/one", "https://x.test/three",
    ]
    assert [item["pageTitle"] for item in results] == ["第一页", "第三页"]
    assert [hit["keyword"] for hit in results[1]["hits"]] == [
        "FIRST-8426", "SECOND-9537",
    ]


def _candidate(
    url, title, *, date=None, score=0,
):
    return Candidate(
        url=url,
        source="freecms-recent",
        title_hint=title,
        published_date=date,
        score=score,
    )


def test_match_body_with_recall_combines_strong_weak_and_ignores_no_match():
    pages = [SimpleNamespace(
        url="https://x.test/strong",
        html="<title>强正文</title><main>正文含 KEY-ONE</main>",
    )]
    candidates = [
        _candidate("https://x.test/strong", "KEY-ONE 标题"),
        _candidate("https://x.test/fetch-failed", "通知 KEY-ONE"),
        _candidate("https://x.test/recent-only", "近期通知但标题无关"),
    ]

    results, strong_hits, weak_results = match_body_with_recall(
        pages, [" KEY-ONE ", "", "KEY-ONE"], candidates,
    )

    assert strong_hits == 1
    assert weak_results == 1
    assert [item["pageUrl"] for item in results] == [
        "https://x.test/strong",
        "https://x.test/fetch-failed",
    ]
    assert [item["matchStrength"] for item in results] == [
        "strong", "weak",
    ]
    weak_hit = results[1]["hits"][0]
    assert weak_hit == {
        "kind": "title-recall",
        "snippet": "通知 KEY-ONE",
        "keyword": "KEY-ONE",
        "href": "https://x.test/fetch-failed",
        "linkHref": None,
    }


def test_match_body_with_recall_strong_canonical_url_suppresses_weak():
    page = SimpleNamespace(
        url="HTTPS://X.TEST/notice/?utm_source=feed#fragment",
        html="<main>正文 TARGET</main>",
    )
    candidates = [
        _candidate("https://x.test/notice", "标题 TARGET"),
        _candidate("https://x.test/notice/", "重复标题 TARGET", score=99),
    ]

    results, strong_hits, weak_results = match_body_with_recall(
        [page], ["TARGET"], candidates,
    )

    assert strong_hits == 1
    assert weak_results == 0
    assert len(results) == 1
    assert results[0]["matchStrength"] == "strong"


def test_match_body_with_recall_counts_weak_results_not_title_hits():
    results, strong_hits, weak_results = match_body_with_recall(
        [],
        [" Alpha ", "Beta", "Alpha", ""],
        [_candidate("https://x.test/two-keywords", "Alpha 与 Beta 通知")],
    )

    assert strong_hits == 0
    assert weak_results == 1
    assert [hit["keyword"] for hit in results[0]["hits"]] == [
        "Alpha", "Beta",
    ]


def test_match_body_with_recall_sorts_and_hides_ranking_metadata():
    pages = [
        SimpleNamespace(
            url="https://x.test/strong-unknown",
            html="<main>MATCH unknown</main>",
        ),
        SimpleNamespace(
            url="https://x.test/strong-low",
            html="<main>MATCH low</main>",
        ),
        SimpleNamespace(
            url="https://x.test/strong-high",
            html="<main>MATCH high</main>",
        ),
    ]
    candidates = [
        _candidate(
            "https://x.test/strong-unknown", "MATCH unknown",
            date=None, score=999,
        ),
        _candidate(
            "https://x.test/strong-low", "MATCH low",
            date="2026-07-20", score=10,
        ),
        _candidate(
            "https://x.test/strong-high", "MATCH high",
            date="2026-07-20", score=80,
        ),
        _candidate(
            "https://x.test/weak-new", "MATCH weak new",
            date="2026-07-21", score=1,
        ),
        _candidate(
            "https://x.test/weak-old", "MATCH weak old",
            date="2026-07-19", score=100,
        ),
        _candidate(
            "https://x.test/weak-unknown", "MATCH weak unknown",
            date=None, score=999,
        ),
    ]

    results, strong_hits, weak_results = match_body_with_recall(
        pages, ["MATCH"], candidates,
    )

    assert strong_hits == 3
    assert weak_results == 3
    assert [item["pageUrl"] for item in results] == [
        "https://x.test/strong-high",
        "https://x.test/strong-low",
        "https://x.test/strong-unknown",
        "https://x.test/weak-new",
        "https://x.test/weak-old",
        "https://x.test/weak-unknown",
    ]
    assert all(set(item) == {
        "pageUrl", "pageTitle", "hits", "matchStrength",
    } for item in results)
