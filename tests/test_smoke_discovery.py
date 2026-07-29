import asyncio
import json

import httpx

from scripts import smoke_discovery
from scripts.smoke_discovery import build_parser, build_payload, summarize_response


class FakeResponse:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def fake_client_class(response, calls):
    class FakeAsyncClient:
        def __init__(self, *, timeout):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, json):
            calls["url"] = url
            calls["payload"] = json
            return response

    return FakeAsyncClient


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


def test_async_main_posts_to_normalized_api_and_prints_safe_summary(
    monkeypatch, capsys
):
    calls = {}
    response = FakeResponse(
        {
            "pagesCrawled": 12,
            "totalHits": 1,
            "discovery": {"partial": False},
            "results": [{"pageUrl": "https://example.com/article"}],
            "cookies": {"session": "secret"},
        }
    )
    monkeypatch.setattr(
        smoke_discovery.httpx, "AsyncClient", fake_client_class(response, calls)
    )

    exit_code = asyncio.run(
        smoke_discovery.async_main(
            [
                "https://example.com/",
                "正文关键词",
                "--api",
                "http://127.0.0.1:7100///",
                "--depth",
                "2",
            ]
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == {
        "timeout": 130,
        "url": "http://127.0.0.1:7100/api/search",
        "payload": {
            "startUrl": "https://example.com/",
            "keywords": ["正文关键词"],
            "depth": 2,
            "render": "auto",
        },
    }
    assert json.loads(captured.out) == {
        "pagesCrawled": 12,
        "totalHits": 1,
        "discovery": {"partial": False},
        "resultUrls": ["https://example.com/article"],
    }
    assert captured.err == ""
    assert "secret" not in captured.out


def test_async_main_reports_http_error_type_without_response_body(
    monkeypatch, capsys
):
    calls = {}
    request = httpx.Request("POST", "http://127.0.0.1:7100/api/search")
    raw_response = httpx.Response(
        500, request=request, text="secret response body"
    )
    error = httpx.HTTPStatusError(
        "secret response body", request=request, response=raw_response
    )
    response = FakeResponse(error=error)
    monkeypatch.setattr(
        smoke_discovery.httpx, "AsyncClient", fake_client_class(response, calls)
    )

    exit_code = asyncio.run(
        smoke_discovery.async_main(["https://example.com/", "正文关键词"])
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "冒烟验证失败：HTTPStatusError\n"
    assert "secret" not in captured.err


def test_async_main_safely_handles_malformed_json_decoder_error(
    monkeypatch, capsys
):
    calls = {}
    response = FakeResponse(data=ValueError("secret response body"))
    monkeypatch.setattr(
        smoke_discovery.httpx, "AsyncClient", fake_client_class(response, calls)
    )

    exit_code = asyncio.run(
        smoke_discovery.async_main(["https://example.com/", "正文关键词"])
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "冒烟验证失败：ValueError\n"
    assert "secret" not in captured.err


def test_async_main_safely_handles_missing_json_fields(monkeypatch, capsys):
    calls = {}
    response = FakeResponse(
        {
            "pagesCrawled": 1,
            "totalHits": 0,
            "results": [],
            "secret": "secret response body",
        }
    )
    monkeypatch.setattr(
        smoke_discovery.httpx, "AsyncClient", fake_client_class(response, calls)
    )

    exit_code = asyncio.run(
        smoke_discovery.async_main(["https://example.com/", "正文关键词"])
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "冒烟验证失败：KeyError\n"
    assert "secret" not in captured.err
