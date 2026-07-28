import json

from scripts.smoke_discovery import build_parser, build_payload, summarize_response


def test_parser_uses_safe_smoke_defaults():
    args = build_parser().parse_args(["https://example.com/", "正文关键词"])

    assert args.api == "http://127.0.0.1:7100"
    assert args.depth == 1
    assert args.start_url == "https://example.com/"
    assert args.keyword == "正文关键词"


def test_payload_requests_automatic_rendering():
    args = build_parser().parse_args(
        ["https://example.com/", "正文关键词", "--depth", "3"]
    )

    assert build_payload(args) == {
        "startUrl": "https://example.com/",
        "keywords": ["正文关键词"],
        "depth": 3,
        "render": "auto",
    }


def test_summary_only_contains_public_diagnostic_fields():
    response = {
        "pagesCrawled": 12,
        "totalHits": 2,
        "discovery": {"partial": True, "providers": {"sitemap": 1}},
        "results": [
            {"pageUrl": "https://example.com/a", "hits": [{"keyword": "正文关键词"}]},
            {"pageUrl": "https://example.com/b", "hits": [{"keyword": "正文关键词"}]},
        ],
        "requestHeaders": {"Authorization": "secret"},
        "cookies": {"session": "secret"},
    }

    output = json.loads(summarize_response(response))

    assert output == {
        "pagesCrawled": 12,
        "totalHits": 2,
        "discovery": {"partial": True, "providers": {"sitemap": 1}},
        "resultUrls": ["https://example.com/a", "https://example.com/b"],
    }
