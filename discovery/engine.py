from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from crawler import CrawledPage, CrawlResult
from discovery.adapters import (
    FreeCmsAdapter,
    YunnanCmsAdapter,
    select_adapter,
)
from discovery.fetcher import DiscoveryFetcher, make_client
from discovery.models import (
    BudgetManager,
    Candidate,
    DiscoveryStats,
    SearchSpec,
)
from discovery.parsers import (
    detect_category_urls,
    detect_feed_urls,
    detect_search_specs,
)
from discovery.providers import (
    CategoryProvider,
    FeedProvider,
    FreeCmsApiProvider,
    FreeCmsRecentProvider,
    SearchProvider,
    SitemapProvider,
    YunnanCmsProvider,
)
from discovery.urltools import (
    extend_policy_with_declared_urls,
    normalize_candidate_url,
)


def merge_candidates(items: list[Candidate]) -> list[Candidate]:
    """Normalize and de-duplicate candidates, retaining the strongest source."""
    best: dict[str, Candidate] = {}
    for item in items:
        normalized = normalize_candidate_url(item.url)
        try:
            parts = urlsplit(normalized)
        except (UnicodeError, ValueError):
            continue
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
        ):
            continue
        current = best.get(normalized)
        evidence = {
            item.source,
            *item.source_evidence,
        }
        if current is not None:
            evidence.update(current.source_evidence)
            selected = item if item.score > current.score else current
            published_date = (
                selected.published_date
                or current.published_date
                or item.published_date
            )
        else:
            selected = item
            published_date = item.published_date
        best[normalized] = Candidate(
            normalized,
            selected.source,
            selected.keyword,
            selected.title_hint,
            selected.score,
            selected.requires_render,
            selected.section,
            published_date,
            tuple(sorted(evidence)),
        )
    return list(best.values())


def rank_candidates(
    items: list[Candidate],
    per_source: int | None = 40,
    per_section: int | None = 40,
) -> list[Candidate]:
    """Rank by relevance with configurable source and section quotas."""
    ranked = sorted(items, key=lambda item: (-item.score, item.url))
    source_counts: dict[str, int] = {}
    section_counts: dict[str, int] = {}
    result: list[Candidate] = []
    for item in ranked:
        if (
            per_source is not None
            and source_counts.get(item.source, 0) >= per_source
        ):
            continue
        if (
            item.section
            and per_section is not None
            and section_counts.get(item.section, 0) >= per_section
        ):
            continue
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
        if item.section:
            section_counts[item.section] = (
                section_counts.get(item.section, 0) + 1
            )
        result.append(item)
    return result


@dataclass
class DiscoveryRun:
    pages: list[CrawledPage]
    failed: list[dict]
    stats: DiscoveryStats
    candidates: list[Candidate] = field(default_factory=list)


def _safe_origin(start_url: str) -> str:
    try:
        parts = urlsplit(start_url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except (TypeError, UnicodeError, ValueError):
        return ""
    if (
        scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
    ):
        return ""
    try:
        normalized_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""
    display_host = (
        f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    )
    port_suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{display_host}{port_suffix}"


def _deduplicate_specs(specs: list[SearchSpec]) -> list[SearchSpec]:
    result: list[SearchSpec] = []
    seen: set[tuple[str, str, str | None]] = set()
    for spec in specs:
        key = (spec.url, spec.query_param, spec.page_param)
        if key in seen:
            continue
        seen.add(key)
        result.append(spec)
    return result


async def _gather_candidates(coroutines, budget: BudgetManager):
    tasks = [asyncio.create_task(item) for item in coroutines]
    if not tasks:
        return [], False
    try:
        done, pending = await asyncio.wait(
            tasks, timeout=budget.remaining_seconds()
        )
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if any(task.cancelled() for task in done):
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise asyncio.CancelledError
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results = [
        task.result()
        for task in tasks
        if task in done
        and not task.cancelled()
        and task.exception() is None
    ]
    return results, bool(pending)


async def discover_pages(
    start_url: str,
    keywords: list[str],
    base_result: CrawlResult,
    depth: int,
    render_mode: str,
    timeout_seconds: float = 120.0,
    started_at: float | None = None,
    skip_urls: Iterable[str] = (),
    budget: BudgetManager | None = None,
) -> DiscoveryRun:
    """Discover and fetch structured candidates independently of BFS depth."""
    del depth
    homepage = base_result.pages[0].html if base_result.pages else ""
    effective_start_url = (
        base_result.pages[0].url if base_result.pages else start_url
    )
    adapter = select_adapter(effective_start_url, homepage)
    policy = adapter.domain_policy(effective_start_url)
    stats = DiscoveryStats(profile=adapter.profile)
    if budget is None:
        if started_at is None:
            started_at = time.monotonic()
        budget = BudgetManager(
            timeout_seconds=timeout_seconds,
            used_html_pages=len(base_result.pages),
            started_at=started_at,
        )

    detected_specs = detect_search_specs(homepage, effective_start_url)
    feed_urls = list(
        dict.fromkeys(detect_feed_urls(homepage, effective_start_url))
    )
    declared_urls = (
        [spec.url for spec in detected_specs] + feed_urls
    )
    if adapter.profile == "generic" and declared_urls:
        policy = extend_policy_with_declared_urls(
            policy,
            declared_urls,
        )
    specs = _deduplicate_specs(
        [*adapter.search_specs(), *detected_specs]
    )
    category_urls = list(
        dict.fromkeys(
            [
                *adapter.category_urls(),
                *detect_category_urls(
                    homepage, effective_start_url, policy
                ),
            ]
        )
    )
    origin = _safe_origin(effective_start_url)

    async with make_client() as client:
        try:
            fetcher_parameters = inspect.signature(
                DiscoveryFetcher
            ).parameters
        except (TypeError, ValueError):
            fetcher_parameters = {}
        supports_policy = (
            "policy" in fetcher_parameters
            or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in fetcher_parameters.values()
            )
        )
        if supports_policy:
            fetcher = DiscoveryFetcher(
                client, budget, stats, policy=policy
            )
        else:
            fetcher = DiscoveryFetcher(client, budget, stats)
        providers = [
            SitemapProvider(client, budget, stats, policy, origin),
            FeedProvider(client, budget, stats, policy, feed_urls),
            CategoryProvider(
                client,
                budget,
                stats,
                policy,
                category_urls,
                fetcher=fetcher,
                render_mode=render_mode,
            ),
        ]
        if isinstance(adapter, FreeCmsAdapter):
            providers.append(
                FreeCmsApiProvider(
                    client, budget, stats, policy, adapter
                )
            )
            if adapter.recent_notice_spec() is not None:
                providers.append(
                    FreeCmsRecentProvider(
                        client, budget, stats, policy, adapter
                    )
                )
        elif isinstance(adapter, YunnanCmsAdapter):
            providers.append(
                YunnanCmsProvider(client, budget, stats, policy, adapter)
            )
        elif specs:
            providers.append(
                SearchProvider(client, budget, stats, policy, specs)
            )

        provider_tasks = [
            asyncio.create_task(provider.discover(keywords))
            for provider in providers
        ]
        try:
            done, pending_tasks = await asyncio.wait(
                provider_tasks,
                timeout=budget.remaining_seconds(),
            )
        except BaseException:
            for task in provider_tasks:
                task.cancel()
            await asyncio.gather(
                *provider_tasks, return_exceptions=True
            )
            raise
        if pending_tasks:
            stats.partial = True
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(
                *pending_tasks, return_exceptions=True
            )
        batches = []
        for task in provider_tasks:
            if task in done and task.cancelled():
                raise asyncio.CancelledError
            if task not in done:
                batches.append(TimeoutError())
                continue
            exception = task.exception()
            batches.append(
                exception if exception is not None else task.result()
            )
        all_candidates: list[Candidate] = []
        for provider, batch in zip(providers, batches):
            if isinstance(batch, asyncio.CancelledError):
                raise batch
            if isinstance(batch, Exception):
                stats.warnings.append(
                    f"{provider.source}: {type(batch).__name__}"
                )
                continue
            all_candidates.extend(batch)

        ranked = rank_candidates(
            merge_candidates(all_candidates),
            per_source=None,
            per_section=None,
        )
        visited = {
            normalize_candidate_url(page.url)
            for page in base_result.pages
        }
        visited.update(
            normalize_candidate_url(url)
            for url in skip_urls
        )
        pending = [item for item in ranked if item.url not in visited]
        stats.candidates_found = len(pending)
        pages: list[CrawledPage] = []
        scheduled_count = 0

        async def fetch(
            item: Candidate,
        ) -> tuple[str, CrawledPage] | None:
            fetch_html_page = getattr(
                fetcher, "fetch_html_page", None
            )
            if fetch_html_page is not None:
                loaded = await fetch_html_page(item.url)
                if loaded is None:
                    return None
                page = CrawledPage(loaded.final_url, loaded.html)
            else:
                html = await fetcher.fetch_html(item.url)
                if html is None:
                    return None
                page = CrawledPage(item.url, html)
            if not normalize_candidate_url(page.url):
                return None
            return item.url, page

        first_capacity = max(
            0, budget.page_limit - budget.used_html_pages
        )
        first_batch = pending[:first_capacity]
        first_success_urls: set[str] = set()
        page_urls = set(visited)

        def append_unique(
            fetched_items: list[tuple[str, CrawledPage]],
            successful_candidates: set[str],
        ) -> None:
            for candidate_url, page in fetched_items:
                successful_candidates.add(candidate_url)
                normalized_page_url = normalize_candidate_url(page.url)
                if normalized_page_url in page_urls:
                    continue
                page_urls.add(normalized_page_url)
                pages.append(page)

        if first_batch:
            fetched, timed_out = await _gather_candidates(
                [fetch(item) for item in first_batch],
                budget,
            )
            stats.partial = stats.partial or timed_out
            successful_items = [
                item for item in fetched if item is not None
            ]
            append_unique(
                successful_items,
                first_success_urls,
            )
            scheduled_count += len(first_batch)

        remaining = [
            *pending[len(first_batch) :],
            *[
                item
                for item in first_batch
                if item.url not in first_success_urls
            ],
        ]
        high_value = list({
            item.url: item
            for item in remaining
            if item.score >= 55
            and item.url not in first_success_urls
        }.values())
        if budget.expand(len(high_value)):
            stats.budget_expanded = True
            second_capacity = max(
                0, budget.page_limit - budget.used_html_pages
            )
            second_batch = high_value[:second_capacity]
            if second_batch:
                fetched, timed_out = await _gather_candidates(
                    [fetch(item) for item in second_batch],
                    budget,
                )
                stats.partial = stats.partial or timed_out
                second_success_urls: set[str] = set()
                append_unique(
                    [item for item in fetched if item is not None],
                    second_success_urls,
                )
                scheduled_count += len(second_batch)

        stats.candidates_fetched = len(pages)
        stats.partial = (
            stats.partial
            or budget.expired()
            or len(pending) > scheduled_count
        )
        stats.elapsed_ms = max(
            0, int((time.monotonic() - budget.started_at) * 1000)
        )
        return DiscoveryRun(
            pages=pages,
            failed=[],
            stats=stats,
            candidates=ranked,
        )
