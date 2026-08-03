# Warning Cleanup and 90 Percent Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the complete test suite warning-free and raise combined line and branch coverage above 90%, with a working target of at least 91%.

**Architecture:** Replace deprecated FastAPI events with one lifespan owner, make Playwright resource ownership explicit, and run real browser tests on one managed event loop. Raise coverage through deterministic boundary tests around provider, renderer, AI, job, server, and discovery-engine behavior; enforce the result in `.coveragerc`.

**Tech Stack:** Python 3.12, FastAPI 0.141, Starlette 1.3, httpx/httpx2, Playwright 1.61, pytest 9.1, coverage.py 7.15.

---

## Baseline

- Complete suite: `486 passed`, with FastAPI deprecation, Starlette TestClient compatibility, Windows asyncio subprocess cleanup, and renderer invalid-escape warnings.
- Combined line and branch coverage: 86%.
- Highest-value gaps: `discovery/providers.py`, `renderer.py`, `server.py`, `ai.py`, `jobs.py`, and `discovery/engine.py`.

### Task 1: Make test dependencies reproducible

**Files:**
- Create: `requirements-dev.txt`
- Modify: `README.md`

- [ ] **Step 1: Add the development dependency file**

```text
beautifulsoup4
coverage>=7.15,<8
fastapi>=0.141,<0.142
httpx>=0.28,<0.29
httpx2
playwright>=1.61,<2
pydantic>=2,<3
pytest>=9.1,<10
python-dotenv
uvicorn[standard]
```

- [ ] **Step 2: Replace the README installation command**

Replace the current long `pip install` command with:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

- [ ] **Step 3: Install and verify the supported TestClient backend**

Run: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`

Run: `.\.venv\Scripts\python.exe -W error -c "from fastapi.testclient import TestClient; print(TestClient.__module__)"`

Expected: install succeeds, import exits 0, and no Starlette warning is emitted.

- [ ] **Step 4: Commit dependency documentation**

```powershell
git add requirements-dev.txt README.md
git commit -m "test: document supported test dependencies"
```

### Task 2: Add failing lifecycle and resource-cleanup regressions

**Files:**
- Create: `tests/test_renderer_lifecycle.py`
- Create: `tests/test_server_lifespan.py`
- Modify: `tests/test_renderer.py`
- Test: `renderer.py`, `server.py`

- [ ] **Step 1: Add renderer cleanup-order tests**

```python
import asyncio

import renderer


class CleanupSpy:
    def __init__(self, name, calls, error=None):
        self.name = name
        self.calls = calls
        self.error = error

    async def close(self):
        self.calls.append(f"{self.name}.close")
        if self.error:
            raise self.error

    async def stop(self):
        self.calls.append(f"{self.name}.stop")
        if self.error:
            raise self.error


def test_shutdown_closes_browser_before_stopping_driver(monkeypatch):
    calls = []
    browser = CleanupSpy("browser", calls)
    driver = CleanupSpy("driver", calls)
    renderer._state.update(browser=browser, pw=driver, loop=None, sem=None)

    asyncio.run(renderer._shutdown_locked())

    assert calls == ["browser.close", "driver.stop"]
    assert renderer._state["browser"] is None
    assert renderer._state["pw"] is None


def test_shutdown_stops_driver_when_browser_close_fails():
    calls = []
    browser = CleanupSpy("browser", calls, RuntimeError("close failed"))
    driver = CleanupSpy("driver", calls)
    renderer._state.update(browser=browser, pw=driver, loop=None, sem=None)

    asyncio.run(renderer._shutdown_locked())

    assert calls == ["browser.close", "driver.stop"]
```

- [ ] **Step 2: Add the FastAPI lifespan cleanup contract**

```python
import asyncio

from fastapi.testclient import TestClient

import renderer
import server


def test_lifespan_cancels_scheduler_then_closes_renderer(monkeypatch):
    events = []

    async def scheduler():
        events.append("scheduler-start")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("scheduler-stop")

    async def shutdown():
        events.append("renderer-stop")

    monkeypatch.setattr(server, "scheduler_loop", scheduler)
    monkeypatch.setattr(renderer, "shutdown", shutdown)

    with TestClient(server.app) as client:
        assert client.get("/").status_code == 200

    assert events == [
        "scheduler-start",
        "scheduler-stop",
        "renderer-stop",
    ]
```

- [ ] **Step 3: Convert real Chromium tests to one managed runner**

Add this module-scoped fixture to `tests/test_renderer.py`:

```python
@pytest.fixture(scope="module")
def render_run():
    runner = asyncio.Runner()
    try:
        yield runner.run
    finally:
        runner.run(renderer.shutdown())
        runner.close()
```

Replace every per-test `asyncio.run` call around `renderer.render_page_result` or `render_page` with the fixture's `render_run` callable, adding `render_run` as a test argument. Keep `_chromium_ok()` independent because its context manager already closes its browser and driver.

- [ ] **Step 4: Verify regression tests fail for the expected reasons**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_renderer_lifecycle.py::test_shutdown_closes_browser_before_stopping_driver -q`

Expected: FAIL because `browser.close` is not called.

Run: `.\.venv\Scripts\python.exe -W error -m pytest tests/test_server_lifespan.py -q`

Expected: FAIL or ERROR while deprecated `on_event` hooks are still registered.

### Task 3: Implement warning-free application and renderer lifecycles

**Files:**
- Modify: `renderer.py:49-105`
- Modify: `server.py:1-55,625-711`
- Test: `tests/test_renderer_lifecycle.py`
- Test: `tests/test_server_lifespan.py`
- Test: `tests/test_renderer.py`

- [ ] **Step 1: Make renderer JavaScript constants raw strings**

Prefix the triple-quoted JavaScript constants containing regex escapes with `r`, preserving `%` formatting:

```python
_FIND_NEXT_JS = r"""() => {
  // existing JavaScript body unchanged
}""" % repr(sorted(_NEXT_TEXTS))
```

Apply the same treatment to every renderer JavaScript constant for which `python -m compileall` reports an invalid escape.

- [ ] **Step 2: Close Browser and driver independently**

Replace `_shutdown_locked` with:

```python
async def _shutdown_locked() -> None:
    browser = _state.get("browser")
    pw = _state.get("pw")
    _state.update(loop=None, pw=None, browser=None, sem=None)
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
```

- [ ] **Step 3: Replace FastAPI event hooks with lifespan ownership**

Add imports:

```python
from contextlib import asynccontextmanager, suppress
```

Define the lifespan before app construction:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task
        import renderer
        await renderer.shutdown()


app = FastAPI(title="站内关键词搜索工具", lifespan=lifespan)
```

Remove `_close_browser`, `_start_scheduler`, and both `@app.on_event` decorators. Keep `scheduler_loop` unchanged.

- [ ] **Step 4: Verify lifecycle tests and warning contracts**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_renderer_lifecycle.py tests/test_server_lifespan.py -q`

Expected: all tests pass.

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_renderer.py -q -W error::pytest.PytestUnraisableExceptionWarning`

Expected: all renderer tests pass with no unraisable warning.

Run: `.\.venv\Scripts\python.exe -W error -m compileall -q ai.py cache.py crawler.py discovery jobs.py locator.py matcher.py renderer.py search_budget.py server.py sitesearch.py`

Expected: exit 0 and no output.

- [ ] **Step 5: Commit lifecycle fixes**

```powershell
git add renderer.py server.py tests/test_renderer.py tests/test_renderer_lifecycle.py tests/test_server_lifespan.py
git commit -m "fix: clean up application and browser lifecycles"
```

### Task 4: Cover provider transport and parsing boundaries

**Files:**
- Create: `tests/test_provider_boundaries.py`
- Test: `discovery/providers.py`

- [ ] **Step 1: Add Provider.get_text behavior tests**

Create helpers and cases using real `httpx.MockTransport`:

```python
import asyncio

import httpx
import pytest

from discovery.models import BudgetManager, DiscoveryStats, DomainPolicy
from discovery.providers import Provider, _parse_search_count, _provider_delay_seconds


POLICY = DomainPolicy("x.test", frozenset({"x.test"}))


class LimiterSpy:
    def __init__(self):
        self.waited = []
        self.limited = []

    async def wait(self, url):
        self.waited.append(url)

    async def report_rate_limited(self, url):
        self.limited.append(url)


async def request_with(handler, *, budget=None, limit=20):
    stats = DiscoveryStats()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Provider(client, budget or BudgetManager(), stats, POLICY)
        provider._rate_limiter = LimiterSpy()
        result = await provider.get_text("https://x.test/start", limit=limit)
        return result, stats, provider._rate_limiter


def test_provider_delay_parses_clamps_and_falls_back(monkeypatch):
    monkeypatch.setenv("PROVIDER_REQUEST_DELAY_MS", "250")
    assert _provider_delay_seconds() == 0.25
    monkeypatch.setenv("PROVIDER_REQUEST_DELAY_MS", "-10")
    assert _provider_delay_seconds() == 0.0
    monkeypatch.setenv("PROVIDER_REQUEST_DELAY_MS", "bad")
    assert _provider_delay_seconds() == 1.0


@pytest.mark.parametrize("status", [429, 503])
def test_get_text_reports_rate_limit_statuses(status):
    def handler(request):
        return httpx.Response(status, request=request)

    result, stats, limiter = asyncio.run(request_with(handler))
    assert result is None
    assert limiter.limited == ["https://x.test/start"]
    assert stats.warnings == ["unknown: HTTPStatusError"]


def test_get_text_rejects_missing_offsite_looping_and_excess_redirects():
    scenarios = [
        (lambda request: httpx.Response(302, request=request), "重定向缺少目标"),
        (lambda request: httpx.Response(302, headers={"location": "https://outside.test/"}, request=request), "重定向目标不在允许范围"),
        (lambda request: httpx.Response(302, headers={"location": "/start"}, request=request), "重定向循环"),
    ]
    for handler, message in scenarios:
        result, stats, _limiter = asyncio.run(request_with(handler))
        assert result is None
        assert message in stats.warnings[0]


def test_get_text_success_records_source_and_final_url():
    def handler(request):
        return httpx.Response(200, text="body", request=request)

    result, stats, limiter = asyncio.run(request_with(handler))
    assert result == ("body", "https://x.test/start")
    assert stats.sources_succeeded == {"unknown"}
    assert limiter.waited == ["https://x.test/start"]
```

- [ ] **Step 2: Add search-count parser matrix**

```python
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"code": 0, "data": [{"Count": 2}, {"Count": "3"}]}', 5),
        ('{"code": 1, "data": []}', None),
        ('{"code": 0, "data": {}}', None),
        ('{"code": 0, "data": [null]}', None),
        ('{"code": 0, "data": [{"Count": "bad"}]}', None),
        ("共 17 条", 17),
        ("x" * 201 + " 17", None),
    ],
)
def test_parse_search_count_boundaries(body, expected):
    assert _parse_search_count(body) == expected
```

- [ ] **Step 3: Run provider tests and coverage**

Run: `.\.venv\Scripts\python.exe -m coverage run --branch --source=discovery.providers -m pytest tests/test_discovery_providers.py tests/test_provider_boundaries.py -q`

Run: `.\.venv\Scripts\python.exe -m coverage report discovery/providers.py`

Expected: tests pass and provider coverage materially exceeds the 81% baseline.

- [ ] **Step 4: Commit provider tests**

```powershell
git add tests/test_provider_boundaries.py
git commit -m "test: cover provider transport boundaries"
```

### Task 5: Cover renderer helpers without launching Chromium

**Files:**
- Create: `tests/test_renderer_boundaries.py`
- Test: `renderer.py`

- [ ] **Step 1: Add fake-page tests for helper success and failure branches**

Implement a `FakePage` with configurable `wait_for_load_state`, `eval_on_selector_all`, `evaluate`, `content`, `goto`, `url`, `route`, and `unroute` methods. Cover these exact contracts:

- `_wait_idle` returns after both load-state operations raise.
- `_collect_links` returns only string URLs and returns an empty list when page evaluation raises.
- `_find_next` returns `None` when JavaScript evaluation raises.
- `_click_next` returns `False` for a missing button or click timeout.
- `browser_page_session` maps `new_page` failure to `RenderError` and always closes a created page after body failure.
- `render_page_result` maps navigation exceptions and HTTP status 400+ to stable `RenderError` messages.
- Navigation guards reject off-policy URLs and report exhausted request budget without issuing the blocked request.
- `_harvest_pagination` stops on repeated page state and on failed clicks while retaining links already collected.

Each fake records calls; assertions verify returned links/content, exact `RenderError` message fragments, route removal, and page closure rather than merely executing the branch.

- [ ] **Step 2: Run renderer unit and real-browser warning checks**

Run: `.\.venv\Scripts\python.exe -m coverage run --branch --source=renderer -m pytest tests/test_renderer_boundaries.py tests/test_renderer_lifecycle.py tests/test_renderer.py -q -W error::pytest.PytestUnraisableExceptionWarning`

Run: `.\.venv\Scripts\python.exe -m coverage report renderer.py`

Expected: all tests pass, no unraisable warnings, and renderer coverage rises substantially above 73%.

- [ ] **Step 3: Commit renderer boundary tests**

```powershell
git add tests/test_renderer_boundaries.py
git commit -m "test: cover renderer failure boundaries"
```

### Task 6: Cover AI, jobs, server, and discovery-engine boundaries

**Files:**
- Create: `tests/test_ai_http.py`
- Create: `tests/test_jobs_boundaries.py`
- Create: `tests/test_discovery_engine_boundaries.py`
- Modify: `tests/test_server_boundaries.py`
- Test: `ai.py`, `jobs.py`, `server.py`, `discovery/engine.py`

- [ ] **Step 1: Add AI HTTP client tests**

Use a captured real `httpx.AsyncClient` plus `MockTransport`, monkeypatch `ai.httpx.AsyncClient` to inject it, and cover valid chat content, HTTP 401, timeout, connection failure, missing choices/message content, invalid JSON, streaming content/done/malformed lines, streaming HTTP failure, successful keyword parsing, and AI-error degradation to an empty expansion. Assert exact `AIError` messages and request payload fields (`model`, `max_tokens`, `stream`, authorization header).

- [ ] **Step 2: Add job boundary tests**

Use temporary files and pure helpers to verify that corrupt JobStore JSON loads as empty, updating a missing ID raises `KeyError`, `_require_crawl_pages` uses the first failure reason or the default reason, new-hit filtering handles empty baselines and mixed old/new hits, structured summaries fall back to a useful snippet when page text is noise, and entry titles fall back from a bare site name to the URL path. Do not touch `data/jobs.json`.

- [ ] **Step 3: Add discovery-engine boundary tests**

Cover `_safe_origin` rejection of credentials, invalid ports, and IDNA failure; SearchSpec deduplication by URL/query/page key; candidate rejection for relative URLs plus best-score/evidence merging; optional source and section quotas; cancellation of every created task when `asyncio.wait` raises; and propagation of a cancelled completed task. Assert normalized URLs, selected candidate metadata, evidence union, deterministic ranking, and completed task cleanup.

- [ ] **Step 4: Add server scheduler and remaining error branches**

Extend `tests/test_server_boundaries.py` to verify `_crawl_or_502` maps timeout and request errors to sanitized 502 responses, `sse_stream` emits an error event after `AIError`, one scheduler iteration runs due jobs and survives a job exception, and `_make_entry_title` follows summary, raw-title, URL-path, then URL fallback order.

- [ ] **Step 5: Run focused coverage reports**

Run:

```powershell
.\.venv\Scripts\python.exe -m coverage run --branch --source=ai,jobs,server,discovery.engine -m pytest `
  tests/test_ai.py tests/test_ai_http.py `
  tests/test_jobs.py tests/test_jobs_helpers.py tests/test_jobs_boundaries.py `
  tests/test_server.py tests/test_server_auto.py tests/test_server_ai.py `
  tests/test_server_jobs.py tests/test_server_boundaries.py tests/test_server_lifespan.py `
  tests/test_discovery_engine.py tests/test_discovery_engine_boundaries.py -q
```

Run: `.\.venv\Scripts\python.exe -m coverage report ai.py jobs.py server.py discovery/engine.py`

Expected: all tests pass and each file improves over its 75%, 87%, 83%, and 85% baseline respectively.

- [ ] **Step 6: Commit boundary tests**

```powershell
git add tests/test_ai_http.py tests/test_jobs_boundaries.py tests/test_discovery_engine_boundaries.py tests/test_server_boundaries.py
git commit -m "test: cover service and discovery boundaries"
```

### Task 7: Enforce warning and coverage gates

**Files:**
- Modify: `.coveragerc`

- [ ] **Step 1: Configure an unambiguous threshold**

Add to `[report]`:

```ini
precision = 2
fail_under = 90.01
```

- [ ] **Step 2: Run all tests with warnings promoted to errors**

Run: `.\.venv\Scripts\python.exe -W error -m pytest tests -q`

Expected: every test passes, no warning summary is printed, and no unraisable exception appears after pytest exits.

- [ ] **Step 3: Run the complete branch-coverage gate**

Run: `.\.venv\Scripts\python.exe -W error -m coverage run -m pytest tests -q`

Run: `.\.venv\Scripts\python.exe -m coverage report`

Expected: all tests pass without warnings; total coverage is at least 91.00% and therefore exceeds `fail_under = 90.01`.

- [ ] **Step 4: Verify compilation and repository scope**

Run: `.\.venv\Scripts\python.exe -W error -m compileall -q ai.py cache.py crawler.py discovery jobs.py locator.py matcher.py renderer.py search_budget.py server.py sitesearch.py`

Run: `git diff --check`

Run: `git status --short`

Expected: compile exits 0 with no output, diff check reports no errors, and task commits contain only dependency documentation, lifecycle fixes, coverage configuration, and tests. Pre-existing unrelated changes remain uncommitted.

- [ ] **Step 5: Commit the coverage gate**

```powershell
git add .coveragerc
git commit -m "test: enforce warning-free 90 percent coverage"
```

- [ ] **Step 6: Report final evidence**

Report exact test count, warning count, total coverage, per-target-file coverage, dependency additions, lifecycle changes, and remaining uncovered high-risk branches. Completion requires fresh outputs from Steps 2, 3, and 5.
