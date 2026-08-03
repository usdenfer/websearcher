# Warning Cleanup and 90 Percent Coverage Design

## Goal

Eliminate warnings emitted by the complete test suite without globally suppressing them, and raise combined line and branch coverage above 90%. Target at least 91% in the final report to provide margin above the configured threshold.

## Current State

The complete suite has 486 passing tests and 86% combined line and branch coverage. Its warning output has four identified sources:

1. FastAPI emits deprecation warnings for `app.on_event("startup")` and `app.on_event("shutdown")`.
2. Starlette 1.3.1 emits a compatibility warning when TestClient falls back from its required `httpx2` package to legacy `httpx`.
3. Renderer tests repeatedly create new event loops with `asyncio.run()` while Playwright browser state remains associated with a previous loop. Windows later reports unclosed subprocess and pipe transports.
4. Renderer JavaScript source strings contain invalid Python escape sequences such as `\s`, producing `SyntaxWarning` when the module is compiled afresh.

The largest remaining coverage gaps are in `discovery/providers.py`, `renderer.py`, `server.py`, `ai.py`, `jobs.py`, and `discovery/engine.py`.

## Application Lifecycle

Replace FastAPI startup and shutdown event decorators with one `asynccontextmanager` lifespan function supplied when constructing the application. The lifespan function starts the scheduler as a named task, yields control to the application, then cancels and awaits the scheduler before shutting down the renderer.

Cancellation is expected during shutdown and must not be reported as an application error. Renderer shutdown still runs if scheduler cancellation raises an unexpected exception. Existing endpoint behavior and scheduler cadence remain unchanged.

## Renderer Resource Ownership

Renderer state owns both the Playwright Browser and Playwright driver. Shutdown captures the current objects, clears shared state, closes the Browser, and then stops the driver. Each cleanup operation is attempted even if the preceding operation fails.

Real Chromium tests use one explicitly managed `asyncio.Runner` for renderer calls instead of a fresh `asyncio.run()` per assertion. Module teardown invokes `renderer.shutdown()` on that same loop before closing the runner. This matches production's single-loop ownership model and prevents cross-loop cleanup attempts.

Renderer JavaScript constants that contain regular-expression escapes become raw strings, or their backslashes are escaped explicitly, so importing or compiling the module emits no `SyntaxWarning`.

## Test Client Dependency

Add `requirements-dev.txt` as the reproducible development and test dependency entrypoint. It includes the libraries documented in the README for tests and adds `coverage` plus Starlette's required `httpx2` package. The application runtime remains independent of this file.

Tests continue importing FastAPI's TestClient compatibility alias. With `httpx2` installed, Starlette uses its supported client implementation and emits no fallback warning.

## Coverage Strategy

Coverage work remains behavior-focused. Add deterministic tests in this order:

1. Provider request, redirect, parsing, pagination, and failure branches using mock transports and fake adapters.
2. Renderer browser startup, page creation, cleanup, routing, pagination, and error mapping using fakes where a real browser is unnecessary.
3. Server lifespan, scheduler cancellation, locate/search failures, SSE errors, and summary/title boundary behavior.
4. AI HTTP error mapping, malformed payloads, streaming parsing, and keyword-expansion degradation.
5. Job-store persistence failures, scheduling boundaries, summary fallbacks, and run-job error paths.
6. Discovery-engine candidate merging, cancellation, empty budgets, and fetch-result deduplication.

Tests must assert observable results, calls, cleanup state, status codes, or sanitized diagnostics. Tests that only execute lines or mirror implementation details are excluded.

Set coverage report precision to two decimal places and `fail_under = 90.01`. The implementation target is at least 91.00% so minor platform-dependent branches do not make the threshold fragile.

## Verification

Verification proceeds from focused to complete:

1. Add a failing lifecycle or cleanup regression test before changing production cleanup code.
2. Run renderer tests with `PytestUnraisableExceptionWarning` promoted to an error.
3. Run server tests with deprecation warnings promoted to errors.
4. Run all fast unit and mocked integration tests.
5. Run the complete suite with warnings promoted to errors.
6. Run the complete suite under branch coverage and require the configured threshold.
7. Compile all production Python modules to confirm no `SyntaxWarning` remains.

Success requires zero warning-summary entries, zero unraisable exceptions, all tests passing, and combined line and branch coverage greater than 90%.

## Workspace Isolation

The current worktree contains pre-existing uncommitted changes. Implementation uses an ignored project-local worktree on a `codex/` branch. The tracked working diff is copied into the isolated worktree so tests cover the actual current code state. New commits contain only warning cleanup, dependency documentation, coverage configuration, and related tests. Existing unrelated changes remain uncommitted and are preserved in the primary worktree.

## Non-Goals

- Suppressing warning categories globally to obtain clean output.
- Rewriting all synchronous tests around a new async test framework.
- Changing crawler, search, matching, or API contracts unrelated to a demonstrated warning or tested defect.
- Requiring public websites or external credentials during tests.
