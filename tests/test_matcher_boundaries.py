from types import SimpleNamespace

from discovery.models import Candidate
from matcher import (
    extract_text,
    match_body_with_recall,
    match_crawl_result,
    match_page,
)


def test_match_page_ignores_empty_optional_attributes():
    html = (
        '<body><img alt="" src="/">'
        '<meta name="description" content="">'
        '<input placeholder="" value="">visible target</body>'
    )
    hits = match_page(html, "https://x.test/", ["target"])
    assert [(hit.kind, hit.keyword) for hit in hits] == [("text", "target")]


def test_extract_text_skips_empty_nodes_and_caps_output():
    html = (
        "<body>   <p>alpha</p><!-- beta -->"
        "<script>gamma</script><p>delta</p></body>"
    )
    assert extract_text(html, limit=7) == "alpha d"


def test_match_crawl_result_skips_empty_pages_and_counts_all_hits():
    pages = [
        SimpleNamespace(
            url="https://x.test/empty",
            html="<title>Empty</title>",
        ),
        SimpleNamespace(
            url="https://x.test/hit",
            html="<title>Hit page</title><p>alpha</p><img alt='beta'>",
        ),
    ]
    results, total = match_crawl_result(pages, ["alpha", "beta"])
    assert total == 2
    assert [row["pageUrl"] for row in results] == ["https://x.test/hit"]
    assert results[0]["pageTitle"] == "Hit page"
    assert {hit["kind"] for hit in results[0]["hits"]} == {
        "text",
        "img-alt",
    }


def test_recall_ignores_invalid_candidate_url_and_invalid_date():
    candidates = [
        Candidate(
            url="https://[::1",
            source="test",
            title_hint="TARGET invalid",
        ),
        Candidate(
            url="https://x.test/weak",
            source="test",
            title_hint="TARGET weak",
            published_date="not-a-date",
        ),
    ]
    results, strong_hits, weak_results = match_body_with_recall(
        [], ["TARGET"], candidates
    )
    assert strong_hits == 0
    assert weak_results == 1
    assert results[0]["pageUrl"] == "https://x.test/weak"
    assert results[0]["publishedDate"] == "not-a-date"


def test_recall_deduplicates_pages_with_same_canonical_url():
    pages = [
        SimpleNamespace(
            url="https://x.test/a",
            html="<main>TARGET first</main>",
        ),
        SimpleNamespace(
            url="https://x.test/a#fragment",
            html="<main>TARGET second</main>",
        ),
    ]
    results, strong_hits, weak_results = match_body_with_recall(
        pages, ["TARGET"], []
    )
    assert strong_hits == 1
    assert weak_results == 0
    assert len(results) == 1
