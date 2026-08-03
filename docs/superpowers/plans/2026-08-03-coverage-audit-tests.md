# Coverage Audit and Missing Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise line and branch coverage with behavior-focused tests, prioritizing keyword extraction, fetch failures, and API boundaries.

**Architecture:** Keep production behavior unchanged and add deterministic characterization tests around existing public and narrowly scoped internal contracts. Use `coverage.py` branch measurement to guide work, local mock transports instead of public network calls, and isolated test modules where existing test files contain unrelated uncommitted edits.

**Tech Stack:** Python 3.12, pytest 9, coverage.py 7, FastAPI TestClient, httpx MockTransport, asyncio.

---

## Baseline

The approved baseline command completed with `455 passed, 10 warnings` and no failures. Coverage is 81% across production code. Priority-file coverage is `matcher.py` 91%, `crawler.py` 87%, `discovery/fetcher.py` 87%, and `server.py` 75%.

### Task 1: Add reproducible branch-coverage configuration

**Files:**
- Create: `.coveragerc`

- [ ] **Step 1: Add the coverage configuration**

```ini
[run]
branch = True
source = .

[report]
show_missing = True
skip_covered = True
omit =
    .venv/*
    tests/*
    docs/*
    scripts/*
```

- [ ] **Step 2: Verify configuration discovery without rerunning the slow suite**

Run: `.\.venv\Scripts\python.exe -m coverage debug config`

Expected: output shows `branch: True`, `show_missing: True`, and the four omit patterns.

- [ ] **Step 3: Commit the configuration**

```powershell
git add .coveragerc
git commit -m "test: configure branch coverage reporting"
```

### Task 2: Cover keyword extraction and aggregation boundaries

**Files:**
- Create: `tests/test_matcher_boundaries.py`
- Read only: `matcher.py`

- [ ] **Step 1: Add characterization tests for empty attributes, text limits, crawl aggregation, invalid candidates, and duplicate pages**

```python
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
    html = "<body>   <p>alpha</p><!-- beta --><script>gamma</script><p>delta</p></body>"
    assert extract_text(html, limit=7) == "alpha d"


def test_match_crawl_result_skips_empty_pages_and_counts_all_hits():
    pages = [
        SimpleNamespace(url="https://x.test/empty", html="<title>Empty</title>"),
        SimpleNamespace(
            url="https://x.test/hit",
            html="<title>Hit page</title><p>alpha</p><img alt='beta'>",
        ),
    ]
    results, total = match_crawl_result(pages, ["alpha", "beta"])
    assert total == 2
    assert [row["pageUrl"] for row in results] == ["https://x.test/hit"]
    assert results[0]["pageTitle"] == "Hit page"
    assert {hit["kind"] for hit in results[0]["hits"]} == {"text", "img-alt"}


def test_recall_ignores_invalid_candidate_url_and_invalid_date():
    candidates = [
        Candidate(url="not-a-url", source="test", title_hint="TARGET invalid"),
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
        SimpleNamespace(url="https://x.test/a", html="<main>TARGET first</main>"),
        SimpleNamespace(url="https://x.test/a#fragment", html="<main>TARGET second</main>"),
    ]
    results, strong_hits, weak_results = match_body_with_recall(
        pages, ["TARGET"], []
    )
    assert strong_hits == 1
    assert weak_results == 0
    assert len(results) == 1
```

- [ ] **Step 2: Run the new matcher tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_matcher_boundaries.py -q`

Expected: all tests pass. A failure means the asserted existing contract differs from implementation; keep the failing case and report it before changing production code.

- [ ] **Step 3: Run matcher coverage and existing matcher tests**

Run: `.\.venv\Scripts\python.exe -m coverage run --branch --source=matcher -m pytest tests/test_matcher.py tests/test_matcher_boundaries.py -q`

Run: `.\.venv\Scripts\python.exe -m coverage report matcher.py`

Expected: both test files pass and `matcher.py` improves from 91%.

- [ ] **Step 4: Commit matcher tests**

```powershell
git add tests/test_matcher_boundaries.py
git commit -m "test: cover matcher boundary behavior"
```

### Task 3: Cover discovery fetcher policy and failure branches

**Files:**
- Create: `tests/test_discovery_fetcher_boundaries.py`
- Read only: `discovery/fetcher.py`

- [ ] **Step 1: Add deterministic fetcher boundary tests**

```python
import asyncio

import httpx

from crawler import FetchedHtml, PageBudgetExhausted, UnsafeRedirect
from discovery.fetcher import (
    DiscoveryFetcher,
    _host_key,
    _sanitize_url,
    _url_parts,
)
from discovery.models import BudgetManager, DiscoveryStats, DomainPolicy


class RateLimiterSpy:
    def __init__(self):
        self.waited = []
        self.rate_limited = []

    async def wait(self, url):
        self.waited.append(url)

    async def report_rate_limited(self, url):
        self.rate_limited.append(url)


def test_url_helpers_reject_invalid_schemes_and_hide_credentials():
    assert _url_parts("ftp://x.test/file") is None
    assert _host_key("http://[bad") == ("<invalid-url>", None)
    assert _sanitize_url("http://[bad") == "<invalid-url>"
    assert _sanitize_url("https://user:secret@x.test:8443/a?q=secret") == (
        "https://x.test:8443/a"
    )


def test_fetch_html_page_short_circuits_disallowed_and_expired_requests():
    async def run():
        stats = DiscoveryStats()
        policy = DomainPolicy("x.test", frozenset({"x.test"}))
        budget = BudgetManager(timeout_seconds=0)
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, budget, stats, policy=policy)
            assert await fetcher.fetch_html_page("https://outside.test/") is None
            assert await fetcher.fetch_html_page("https://x.test/") is None
        assert stats.partial is True

    asyncio.run(run())


def test_fetch_html_page_maps_known_failures_without_leaking_details(monkeypatch):
    async def run_case(exc):
        async def fail(*args, **kwargs):
            raise exc

        monkeypatch.setattr("discovery.fetcher.fetch_html_retry", fail)
        stats = DiscoveryStats()
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            fetcher._rate_limiter = RateLimiterSpy()
            result = await fetcher.fetch_html_page("https://user:secret@x.test/a?q=secret")
        return result, stats, fetcher._rate_limiter

    for exc, expected in [
        (PageBudgetExhausted("private"), None),
        (UnsafeRedirect("private"), "重定向目标不在允许范围"),
        (ValueError("private"), "非 HTML 内容"),
    ]:
        result, stats, limiter = asyncio.run(run_case(exc))
        assert result is None
        assert "secret" not in " ".join(stats.warnings)
        if expected:
            assert expected in stats.warnings[0]
        else:
            assert stats.partial is True


def test_fetch_html_page_reports_429_to_rate_limiter(monkeypatch):
    request = httpx.Request("GET", "https://x.test/a")
    response = httpx.Response(429, request=request)

    async def fail(*args, **kwargs):
        raise httpx.HTTPStatusError("private", request=request, response=response)

    monkeypatch.setattr("discovery.fetcher.fetch_html_retry", fail)

    async def run():
        stats = DiscoveryStats()
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            limiter = RateLimiterSpy()
            fetcher._rate_limiter = limiter
            assert await fetcher.fetch_html_page("https://x.test/a") is None
        assert limiter.rate_limited == ["https://x.test/a"]
        assert stats.warnings == ["https://x.test/a: HTTPStatusError"]

    asyncio.run(run())


def test_fetch_html_page_rejects_offsite_final_url(monkeypatch):
    async def load(*args, **kwargs):
        return FetchedHtml("<main>ok</main>", "https://outside.test/final")

    monkeypatch.setattr("discovery.fetcher.fetch_html_retry", load)

    async def run():
        policy = DomainPolicy("x.test", frozenset({"x.test"}))
        async with httpx.AsyncClient() as client:
            fetcher = DiscoveryFetcher(
                client, BudgetManager(), DiscoveryStats(), policy=policy
            )
            assert await fetcher.fetch_html_page("https://x.test/start") is None

    asyncio.run(run())
```

- [ ] **Step 2: Run the new and existing fetcher tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_discovery_fetcher.py tests/test_discovery_fetcher_boundaries.py -q`

Expected: all tests pass.

- [ ] **Step 3: Measure fetcher coverage**

Run: `.\.venv\Scripts\python.exe -m coverage run --branch --source=discovery.fetcher -m pytest tests/test_discovery_fetcher.py tests/test_discovery_fetcher_boundaries.py -q`

Run: `.\.venv\Scripts\python.exe -m coverage report discovery/fetcher.py`

Expected: `discovery/fetcher.py` improves from 87%, including invalid URL, expired budget, offsite final URL, and 429 branches.

- [ ] **Step 4: Commit fetcher tests**

```powershell
git add tests/test_discovery_fetcher_boundaries.py
git commit -m "test: cover discovery fetch failure boundaries"
```

### Task 4: Cover crawler redirects, errors, deadlines, and cancellation cleanup

**Files:**
- Create: `tests/test_crawler_boundaries.py`
- Read only: `crawler.py`

- [ ] **Step 1: Add crawler error and deadline tests**

```python
import asyncio
import time

import httpx
import pytest

import crawler
from crawler import (
    MAX_REDIRECT_HOPS,
    await_before_deadline,
    describe_error,
    fetch_html_response,
    gather_before_deadline,
)


def test_describe_error_maps_user_safe_categories():
    request = httpx.Request("GET", "https://x.test/")
    response = httpx.Response(503, request=request)
    assert describe_error(httpx.HTTPStatusError("private", request=request, response=response)) == "HTTP 503"
    assert describe_error(httpx.ReadTimeout("private", request=request)) == "访问超时"
    assert describe_error(httpx.ConnectError("private", request=request)) == "连接失败"
    assert describe_error(ValueError("公开原因")) == "公开原因"
    assert describe_error(RuntimeError("private")) == "RuntimeError"


def test_fetch_html_response_stops_redirect_loop():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "/again"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.TooManyRedirects):
                await fetch_html_response(
                    client,
                    "https://x.test/start",
                    reserve_request=lambda: True,
                    redirect_allowed=lambda url: True,
                )

    asyncio.run(run())
    assert calls == MAX_REDIRECT_HOPS + 1


def test_await_before_deadline_closes_unstarted_coroutine():
    closed = False

    async def operation():
        nonlocal closed
        try:
            await asyncio.sleep(1)
        finally:
            closed = True

    result, expired = asyncio.run(
        await_before_deadline(operation(), time.monotonic() - 1)
    )
    assert result is None
    assert expired is True
    assert closed is False


def test_gather_before_deadline_cancels_all_tasks_when_wait_fails(monkeypatch):
    cancelled = 0

    async def operation():
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled += 1
            raise

    async def broken_wait(*args, **kwargs):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(crawler.asyncio, "wait", broken_wait)
    with pytest.raises(RuntimeError, match="wait failed"):
        asyncio.run(gather_before_deadline([operation(), operation()], None))
    assert cancelled == 2


def test_await_before_deadline_cancels_task_when_wait_fails(monkeypatch):
    cancelled = False

    async def operation():
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def broken_wait(*args, **kwargs):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(crawler.asyncio, "wait", broken_wait)
    with pytest.raises(RuntimeError, match="wait failed"):
        asyncio.run(await_before_deadline(operation(), None))
    assert cancelled is True
```

- [ ] **Step 2: Run crawler boundary tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_crawler_boundaries.py -q`

Expected: all tests pass without live network access.

- [ ] **Step 3: Run complete crawler tests and measure coverage**

Run: `.\.venv\Scripts\python.exe -m coverage run --branch --source=crawler -m pytest tests/test_crawler.py tests/test_crawler_boundaries.py tests/test_crawler_render.py tests/test_archive.py -q`

Run: `.\.venv\Scripts\python.exe -m coverage report crawler.py`

Expected: crawler tests pass and `crawler.py` improves from 87%.

- [ ] **Step 4: Commit crawler tests**

```powershell
git add tests/test_crawler_boundaries.py
git commit -m "test: cover crawler error and deadline paths"
```

### Task 5: Cover API validation, error mapping, summaries, and job endpoints

**Files:**
- Create: `tests/test_server_boundaries.py`
- Read only: `server.py`

- [ ] **Step 1: Add API and helper boundary tests**

```python
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import server
from crawler import CrawlResult
from jobs import JobStore


client = TestClient(server.app)


def test_filter_off_topic_handles_empty_english_and_chinese_terms():
    assert server._filter_off_topic(["教育"], []) == []
    assert server._filter_off_topic(["school"], ["travel", "academy"]) == [
        "travel", "academy"
    ]
    assert server._filter_off_topic(["教育"], ["学校教育", "医疗服务"]) == [
        "学校教育"
    ]


def test_summary_helpers_filter_noise_and_build_sorted_entries():
    pages = [
        {"pageUrl": "", "hits": [{"keyword": "教育"}]},
        {"pageUrl": "https://x.test/no-hits", "hits": []},
        {
            "pageUrl": "https://x.test/short",
            "pageTitle": "短词",
            "hits": [{"keyword": "x", "snippet": "x"}],
        },
        {
            "pageUrl": "https://x.test/project.html",
            "pageTitle": "某政府采购网",
            "publishedDate": "2026-08-03",
            "hits": [{"keyword": "教育采购", "snippet": "教育设备采购项目公告"}],
        },
    ]
    texts = {
        "https://x.test/project.html": (
            "首页\n咨询电话 010-12345678\n"
            "项目名称：某学校智慧教室设备采购项目。"
            "预算金额：120万元。计划采购时间：2026年9月"
        )
    }
    entries = server._build_entries_from_pages(pages, texts)
    assert len(entries) == 1
    assert entries[0]["date"] == "2026-08-03"
    assert entries[0]["link"] == "https://x.test/project.html"
    assert "120万元" in entries[0]["summary"]
    assert "咨询电话" not in entries[0]["summary"]


@pytest.mark.parametrize("path", [
    "/api/jobs/missing/toggle",
    "/api/jobs/missing/run",
])
def test_missing_job_post_endpoints_return_404(monkeypatch, tmp_path, path):
    monkeypatch.setattr(server, "job_store", JobStore(tmp_path / "jobs.json"))
    response = client.post(path)
    assert response.status_code == 404
    assert response.json()["detail"] == "任务不存在"


def test_missing_job_delete_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "job_store", JobStore(tmp_path / "jobs.json"))
    response = client.delete("/api/jobs/missing")
    assert response.status_code == 404


def test_locate_maps_http_and_transport_failures(monkeypatch):
    request = httpx.Request("GET", "https://x.test/page")
    response = httpx.Response(503, request=request)

    async def http_failure(*args, **kwargs):
        raise httpx.HTTPStatusError("private", request=request, response=response)

    monkeypatch.setattr(server, "fetch_html", http_failure)
    result = client.get("/api/locate", params={"url": request.url, "keyword": "x"})
    assert result.status_code == 502
    assert result.json()["detail"] == "目标页返回 HTTP 503"

    async def connect_failure(*args, **kwargs):
        raise httpx.ConnectError("private", request=request)

    monkeypatch.setattr(server, "fetch_html", connect_failure)
    result = client.get("/api/locate", params={"url": request.url, "keyword": "x"})
    assert result.status_code == 502
    assert result.json()["detail"] == "目标页无法访问：连接失败"


def test_archive_search_maps_failure_and_empty_result(monkeypatch):
    async def no_expand(*args, **kwargs):
        return []

    monkeypatch.setattr(server, "expand_keywords", no_expand)
    request = server.SearchRequest(
        startUrl="https://x.test/", keywords=["alpha"], render="archive"
    )

    async def fail_archive(*args, **kwargs):
        raise ValueError("archive failed")

    monkeypatch.setattr(server, "crawl_archive", fail_archive)
    with pytest.raises(server.HTTPException) as raised:
        asyncio.run(server.search(request))
    assert raised.value.status_code == 502
    assert raised.value.detail == "起始页无法访问：archive failed"

    async def empty_archive(*args, **kwargs):
        return CrawlResult()

    monkeypatch.setattr(server, "crawl_archive", empty_archive)
    with pytest.raises(server.HTTPException) as raised:
        asyncio.run(server.search(request))
    assert raised.value.status_code == 502
    assert raised.value.detail == "起始页无法访问，无法归档深扫"
```

- [ ] **Step 2: Run the new API boundary tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_server_boundaries.py -q`

Expected: all tests pass. Any failing status or response contract is reported before production changes.

- [ ] **Step 3: Run server-related tests and measure coverage**

Run: `.\.venv\Scripts\python.exe -m coverage run --branch --source=server -m pytest tests/test_server.py tests/test_server_auto.py tests/test_server_ai.py tests/test_server_jobs.py tests/test_server_boundaries.py -q`

Run: `.\.venv\Scripts\python.exe -m coverage report server.py`

Expected: server-related tests pass and `server.py` improves from 75%.

- [ ] **Step 4: Commit API tests**

```powershell
git add tests/test_server_boundaries.py
git commit -m "test: cover API and summary boundaries"
```

### Task 6: Cover high-yield repository-wide gaps

**Files:**
- Create: `tests/test_ratelimit.py`
- Create: `tests/test_jobs_helpers.py`
- Create: `tests/test_sitesearch_boundaries.py`
- Read only: `discovery/ratelimit.py`, `jobs.py`, `sitesearch.py`

- [ ] **Step 1: Add rate-limiter tests**

```python
import asyncio

import discovery.ratelimit as ratelimit


def test_rate_limiter_host_rules_and_invalid_urls():
    limiter = ratelimit.PerHostRateLimiter(
        default_rate=2.0, overrides={"gov.test": 0.5}
    )
    assert limiter._rate_for("a.gov.test") == 0.5
    assert limiter._rate_for("other.test") == 2.0
    assert limiter._extract_host("http://[bad") == ""
    asyncio.run(limiter.wait("not-a-url"))
    asyncio.run(limiter.wait("https://other.test/"))


def test_rate_limiter_records_caps_and_decays_cooldown(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(ratelimit.asyncio, "sleep", fake_sleep)
    limiter = ratelimit.PerHostRateLimiter()

    async def run():
        for _ in range(10):
            await limiter.report_rate_limited("https://x.test/a")
        assert limiter.rate_limit_events("x.test") == 10
        assert limiter._cooldowns["x.test"] == 300.0
        await limiter.cooldown_if_needed("https://x.test/a")
        await limiter.cooldown_if_needed("not-a-url")

    asyncio.run(run())
    assert sleeps == [300.0]
    assert limiter._cooldowns["x.test"] == 240.0


def test_get_rate_limiter_is_singleton(monkeypatch):
    monkeypatch.setattr(ratelimit, "_rate_limiter", None)

    async def run():
        first = await ratelimit.get_rate_limiter()
        second = await ratelimit.get_rate_limiter()
        assert first is second

    asyncio.run(run())
```

- [ ] **Step 2: Add job-summary helper tests**

```python
import asyncio
from types import SimpleNamespace

import jobs


def test_job_summary_filters_invalid_rows_and_sorts_entries():
    results = [
        {"pageUrl": "", "hits": [{"snippet": "ignored"}]},
        {"pageUrl": "https://x.test/empty", "hits": []},
        {
            "pageUrl": "https://x.test/project",
            "pageTitle": "某政府采购网",
            "publishedDate": "2026-08-03",
            "hits": [{"snippet": "学校设备采购项目", "keyword": "学校"}],
        },
    ]
    pages = [SimpleNamespace(
        url="https://x.test/project",
        html=("<main>项目名称：某学校智慧教室设备采购项目。"
              "预算金额：80万元。计划采购时间：2026年10月</main>"),
    )]
    entries = jobs._build_job_summary_entries(results, pages)
    assert len(entries) == 1
    assert entries[0]["date"] == "2026-08-03"
    assert "80万元" in entries[0]["summary"]


def test_job_date_and_time_filters_keep_unknown_dates():
    since = jobs._parse_published_date("2026-08-01")
    assert jobs._parse_published_date("2026/08/03") is not None
    assert jobs._parse_published_date("bad") is None
    rows = [
        {"pageUrl": "old", "publishedDate": "2026-07-01"},
        {"pageUrl": "new", "publishedDate": "2026-08-03"},
        {"pageUrl": "unknown"},
    ]
    assert [row["pageUrl"] for row in jobs._filter_results_by_time(rows, since)] == [
        "new", "unknown"
    ]


def test_generate_job_ai_summary_handles_empty_success_and_failure(monkeypatch):
    assert asyncio.run(jobs._generate_job_ai_summary(["x"], [])) is None

    async def fake_chat(*args, **kwargs):
        return "【概述】\n概述\n\n【条目】\n[2026-08-03]《标题》\n摘要\nhttps://x.test/a"

    monkeypatch.setattr("ai.chat", fake_chat)
    result = asyncio.run(jobs._generate_job_ai_summary(
        ["x"], [{"pageUrl": "https://x.test/a", "pageTitle": "A", "hits": []}]
    ))
    assert result["overview"] == "概述"
    assert result["entries"][0]["title"] == "标题"

    async def broken_chat(*args, **kwargs):
        raise RuntimeError("private")

    monkeypatch.setattr("ai.chat", broken_chat)
    assert asyncio.run(jobs._generate_job_ai_summary(
        ["x"], [{"pageUrl": "https://x.test/a", "pageTitle": "A", "hits": []}]
    )) is None
```

- [ ] **Step 3: Add legacy site-search error tests**

```python
import asyncio

import httpx
import pytest

import sitesearch
from crawler import CrawlResult


def test_probe_caches_failure_without_raising():
    sitesearch._probe_cache.clear()

    def handler(request):
        raise httpx.ConnectError("private", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await sitesearch.probe(client, "https://x.test") is False
            assert await sitesearch.probe(client, "https://x.test") is False

    asyncio.run(run())
    assert sitesearch._probe_cache == {"https://x.test": False}


def test_collect_pages_returns_empty_for_crawl_and_discovery_failures(monkeypatch):
    async def crawl_failure(*args, **kwargs):
        raise RuntimeError("private")

    monkeypatch.setattr(sitesearch, "crawl", crawl_failure)
    assert asyncio.run(sitesearch.collect_pages("https://x.test", ["x"]))[0] == []

    async def empty_crawl(*args, **kwargs):
        return CrawlResult()

    monkeypatch.setattr(sitesearch, "crawl", empty_crawl)
    assert asyncio.run(sitesearch.collect_pages("https://x.test", ["x"]))[0] == []


def test_collect_pages_propagates_cancellation(monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(sitesearch, "crawl", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sitesearch.collect_pages("https://x.test", ["x"]))
```

- [ ] **Step 4: Run repository-wide gap tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ratelimit.py tests/test_jobs_helpers.py tests/test_sitesearch_boundaries.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit repository-wide tests**

```powershell
git add tests/test_ratelimit.py tests/test_jobs_helpers.py tests/test_sitesearch_boundaries.py
git commit -m "test: cover rate limit and compatibility helpers"
```

### Task 7: Final regression and coverage verification

**Files:**
- Verify: `.coveragerc`
- Verify: all new test modules
- Do not modify unrelated dirty files

- [ ] **Step 1: Run all fast priority tests together**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_matcher.py tests/test_matcher_boundaries.py `
  tests/test_discovery_fetcher.py tests/test_discovery_fetcher_boundaries.py `
  tests/test_crawler.py tests/test_crawler_boundaries.py `
  tests/test_server.py tests/test_server_auto.py tests/test_server_ai.py `
  tests/test_server_jobs.py tests/test_server_boundaries.py `
  tests/test_ratelimit.py tests/test_jobs_helpers.py `
  tests/test_sitesearch.py tests/test_sitesearch_boundaries.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run the complete suite under branch coverage**

Run: `.\.venv\Scripts\python.exe -m coverage run -m pytest tests -q`

Expected: all baseline and new tests pass. Existing deprecation and subprocess cleanup warnings may remain and must be reported, not hidden.

- [ ] **Step 3: Capture the final coverage report**

Run: `.\.venv\Scripts\python.exe -m coverage report`

Expected: total coverage exceeds the 81% baseline and each priority file is no lower than its baseline value.

- [ ] **Step 4: Check the exact repository diff**

Run: `git status --short`

Run: `git diff --check`

Expected: no whitespace errors; changes from this effort are limited to `.coveragerc`, the plan, and the new test files. Existing unrelated modifications remain unstaged unless the user explicitly requests otherwise.

- [ ] **Step 5: Report evidence**

Report the exact test count, failures, warnings, total baseline-to-final coverage change, per-priority-file coverage change, new test files, and any uncovered high-risk branches. Do not claim completion without the fresh outputs from Steps 2–4.
