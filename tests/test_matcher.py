"""matcher.py 的单元测试：覆盖全部 kind、大小写、片段、去重。"""
from matcher import (SNIPPET_RADIUS, extract_title, looks_js_driven,
                    make_snippet, match_page)

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
