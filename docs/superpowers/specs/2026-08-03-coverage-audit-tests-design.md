# Coverage Audit and Missing Tests Design

## Goal

Measure the repository's current line and branch coverage, then add behavior-focused tests that raise coverage as much as practical while prioritizing keyword extraction, web fetching and error handling, and API boundary behavior.

## Scope

The audit covers the full Python repository. Work is prioritized in this order:

1. `matcher.py`: keyword matching and extraction behavior.
2. `crawler.py` and `discovery/fetcher.py`: fetching, redirects, retries, cancellation, deadlines, budgets, response validation, and error reporting.
3. `server.py`: request validation, API status codes, downstream error mapping, response shapes, and compatibility boundaries.
4. Remaining production modules where coverage data identifies high-value gaps.

Existing uncommitted workspace changes are part of the code state being audited. They must be preserved. Test work may edit relevant test files and add narrowly scoped test support or coverage configuration, but must not overwrite unrelated changes.

## Measurement Strategy

Install `coverage.py` into the existing local virtual environment without treating it as an application runtime dependency. Run the existing pytest suite with line and branch measurement enabled and record the baseline totals and missing branches by file.

Use coverage data together with source review. Coverage percentage alone does not establish test quality: every new test must assert externally meaningful behavior or a well-defined internal contract. Prefer tests that exercise several previously uncovered lines only when those lines belong to one coherent behavior.

## Test Design

### Keyword extraction and matching

Add tests for uncovered combinations of empty or duplicate keywords, whitespace normalization, case and Unicode behavior, malformed or partial HTML, hidden and noisy nodes, missing titles or bodies, snippet boundaries, canonical URL deduplication, and recall ordering. Assertions must cover returned hit type, keyword, URL, snippet, ordering, and deduplication as applicable.

### Fetching and error handling

Add deterministic tests using local fixtures or controlled HTTP transports. Cover timeout and deadline behavior, transient retries and retry exhaustion, redirect policy checks, malformed or non-HTML responses, shared page-budget exhaustion, cancellation propagation, sanitization of diagnostic messages, per-host concurrency keys, and cleanup of pending work. Do not depend on public network availability.

### API boundaries

Exercise FastAPI endpoints through the existing test client patterns. Cover missing, empty, malformed, and extreme request values; render-mode validation; downstream crawler, discovery, and AI failures; stable status-code mapping; response field compatibility; streaming error events; and job endpoint not-found or invalid-state paths where coverage shows gaps.

### Repository-wide sweep

After the priority modules, inspect the coverage report for remaining production files. Add tests for high-value uncovered branches with stable behavior. Avoid artificial tests that merely import modules, assert constants without behavioral value, or tightly mirror implementation details.

## Handling Discovered Defects

The requested change is test coverage, not a general behavior rewrite. If a new test exposes a genuine defect in existing behavior, preserve the failing test and report the defect before changing production code, unless the fix is necessary to make the documented existing contract pass and is trivial and unambiguous. Any production fix must follow a red-green test cycle and remain narrowly scoped.

## Verification

Use a staged verification loop:

1. Run each new test in isolation and confirm it fails for the intended missing-coverage or defect reason where applicable.
2. Run the affected module's test file after each group of additions.
3. Re-run line and branch coverage for the priority modules to guide the next additions.
4. Run the complete pytest suite.
5. Run the final complete line and branch coverage command and compare it with the recorded baseline.

The final report will include baseline and final coverage, coverage change by priority file, number of added tests, full-suite pass/fail counts, warnings that remain, and any high-risk paths intentionally left uncovered.

## Non-Goals

- Refactoring production code solely to make the coverage number easier to raise.
- Adding tests that require live third-party websites or credentials.
- Changing API contracts or crawler behavior without a separately identified defect and regression test.
- Including unrelated workspace changes in commits made for this audit.
