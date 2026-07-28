from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
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
        if current is not None and current.score >= item.score:
            continue
        best[normalized] = Candidate(
            normalized,
            item.source,
            item.keyword,
            item.title_hint,
            item.score,
            item.requires_render,
            item.section,
        )
    return list(best.values())


def rank_candidates(
    items: list[Candidate],
    per_source: int = 40,
    per_section: int = 40,
) -> list[Candidate]:
    """Rank by relevance while bounding any one source or section."""
    ranked = sorted(items, key=lambda item: (-item.score, item.url))
    source_counts: dict[str, int] = {}
    section_counts: dict[str, int] = {}
    result: list[Candidate] = []
    for item in ranked:
        section = item.section or "_"
        if source_counts.get(item.source, 0) >= per_source:
            continue
        if section_counts.get(section, 0) >= per_section:
            continue
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
        section_counts[section] = section_counts.get(section, 0) + 1
        result.append(item)
    return result


@dataclass
class DiscoveryRun:
    pages: list[CrawledPage]
    failed: list[dict]
    stats: DiscoveryStats


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


async def discover_pages(
    start_url: str,
    keywords: list[str],
    base_result: CrawlResult,
    depth: int,
    render_mode: str,
    timeout_seconds: float = 120.0,
    started_at: float | None = None,
) -> DiscoveryRun:
    """Discover and fetch structured candidates independently of BFS depth."""
    del depth
    homepage = base_result.pages[0].html if base_result.pages else ""
    adapter = select_adapter(start_url, homepage)
    policy = adapter.domain_policy(start_url)
    stats = DiscoveryStats(profile=adapter.profile)
    if started_at is None:
        started_at = time.monotonic()
    budget = BudgetManager(
        timeout_seconds=timeout_seconds,
        used_html_pages=len(base_result.pages),
        started_at=started_at,
    )

    detected_specs = detect_search_specs(homepage, start_url)
    feed_urls = list(
        dict.fromkeys(detect_feed_urls(homepage, start_url))
    )
    if adapter.profile == "generic":
        policy = extend_policy_with_declared_urls(
            policy,
            [spec.url for spec in detected_specs] + feed_urls,
        )
    specs = _deduplicate_specs(
        [*adapter.search_specs(), *detected_specs]
    )
    category_urls = list(
        dict.fromkeys(
            [
                *adapter.category_urls(),
                *detect_category_urls(homepage, start_url, policy),
            ]
        )
    )
    origin = _safe_origin(start_url)

    async with make_client() as client:
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
        elif isinstance(adapter, YunnanCmsAdapter):
            providers.append(
                YunnanCmsProvider(client, budget, stats, policy, adapter)
            )
        elif specs:
            providers.append(
                SearchProvider(client, budget, stats, policy, specs)
            )

        batches = await asyncio.gather(
            *(provider.discover(keywords) for provider in providers),
            return_exceptions=True,
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

        ranked = rank_candidates(merge_candidates(all_candidates))
        visited = {
            normalize_candidate_url(page.url)
            for page in base_result.pages
        }
        pending = [item for item in ranked if item.url not in visited]
        stats.candidates_found = len(pending)
        pages: list[CrawledPage] = []
        scheduled_count = 0

        async def fetch(item: Candidate) -> CrawledPage | None:
            html = await fetcher.fetch_html(item.url)
            if html is None:
                return None
            return CrawledPage(item.url, html)

        first_capacity = max(
            0, budget.page_limit - budget.used_html_pages
        )
        first_batch = pending[:first_capacity]
        if first_batch:
            fetched = await asyncio.gather(
                *(fetch(item) for item in first_batch)
            )
            pages.extend(page for page in fetched if page is not None)
            scheduled_count += len(first_batch)

        remaining = pending[len(first_batch) :]
        high_value = [item for item in remaining if item.score >= 55]
        if budget.expand(len(high_value)):
            stats.budget_expanded = True
            second_capacity = max(
                0, budget.page_limit - budget.used_html_pages
            )
            second_batch = high_value[:second_capacity]
            if second_batch:
                fetched = await asyncio.gather(
                    *(fetch(item) for item in second_batch)
                )
                pages.extend(
                    page for page in fetched if page is not None
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
        return DiscoveryRun(pages=pages, failed=[], stats=stats)
