from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import httpx

from discovery.adapters import FreeCmsAdapter, YngpAdapter, YunnanCmsAdapter
from discovery.models import (
    BudgetManager,
    Candidate,
    DiscoveryStats,
    DomainPolicy,
    SearchSpec,
)
from discovery.parsers import (
    parse_feed,
    parse_pagination,
    parse_result_candidates,
    parse_sitemap,
    parse_sitemap_index,
)
from discovery.urltools import normalize_candidate_url, url_allowed
from matcher import looks_js_driven


def _provider_delay_seconds() -> float:
    try:
        ms = float(os.environ.get("PROVIDER_REQUEST_DELAY_MS", "1000").strip())
        return max(ms / 1000.0, 0.0)
    except (ValueError, TypeError):
        return 1.0


class Provider:
    source = "unknown"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
    ):
        self.client = client
        self.budget = budget
        self.stats = stats
        self.policy = policy
        self._rate_limiter = None

    async def _ensure_rate_limiter(self) -> None:
        if self._rate_limiter is not None:
            return
        from discovery.ratelimit import get_rate_limiter
        self._rate_limiter = await get_rate_limiter()

    async def get_text(
        self,
        url: str,
        *,
        limit: int = 500,
        params: dict | None = None,
        counts_as_html: bool = True,
        source: str | None = None,
        budget_source: str | None = None,
    ) -> tuple[str, str] | None:
        source = source or self.source
        self.stats.sources_tried.add(source)
        budget_key = budget_source or source
        current_url = url
        current_params = params
        initial_normalized = normalize_candidate_url(url)
        visited: set[str] = (
            {initial_normalized} if initial_normalized else set()
        )

        for hop in range(11):
            provider_used = self.budget.provider_requests.get(
                budget_key, 0
            )
            remaining = self.budget.remaining_seconds()
            if remaining <= 0:
                self.stats.partial = True
                return None
            if provider_used >= limit:
                return None
            if (
                counts_as_html
                and self.budget.used_html_pages >= self.budget.page_limit
            ):
                self.stats.partial = True
                return None

            # Both reservations are synchronous and contain no await, so the
            # checks and increments form one event-loop-atomic decision.
            if not self.budget.reserve_provider(budget_key, limit):
                return None
            if counts_as_html and not self.budget.reserve_html():
                self.stats.partial = True
                return None

            await self._ensure_rate_limiter()
            await self._rate_limiter.wait(current_url)
            try:
                async with asyncio.timeout(remaining):
                    response = await self.client.get(
                        current_url,
                        params=current_params,
                        follow_redirects=False,
                    )
            except TimeoutError:
                self.stats.partial = True
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.warnings.append(
                    f"{source}: {type(exc).__name__}"
                )
                return None

            location = response.headers.get("location")
            if response.is_redirect:
                if not location:
                    self.stats.warnings.append(
                        f"{source}: 重定向缺少目标"
                    )
                    return None
                if hop >= 10:
                    self.stats.warnings.append(
                        f"{source}: 重定向次数过多"
                    )
                    return None
                target = urljoin(str(response.url), location)
                normalized = normalize_candidate_url(target)
                if not normalized or not url_allowed(target, self.policy):
                    self.stats.warnings.append(
                        f"{source}: 重定向目标不在允许范围"
                    )
                    return None
                if normalized in visited:
                    self.stats.warnings.append(
                        f"{source}: 重定向循环"
                    )
                    return None
                visited.add(normalized)
                current_url = target
                current_params = None
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 429, 503):
                    await self._rate_limiter.report_rate_limited(
                        current_url or str(response.url)
                    )
                self.stats.warnings.append(
                    f"{source}: {type(exc).__name__}"
                )
                return None
            except Exception as exc:
                self.stats.warnings.append(
                    f"{source}: {type(exc).__name__}"
                )
                return None
            self.stats.sources_succeeded.add(source)
            return response.text, str(response.url)

        self.stats.warnings.append(f"{source}: 重定向次数过多")
        return None


class SearchProvider(Provider):
    source = "site-search"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        specs: list[SearchSpec],
    ):
        super().__init__(client, budget, stats, policy)
        self.specs = specs

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        for spec in self.specs:
            for keyword in keywords[:6]:
                loaded = await self.get_text(
                    spec.url,
                    limit=500,
                    params=spec.params_for(keyword),
                    source=spec.source,
                )
                if loaded is None:
                    continue
                html, final_url = loaded
                initial_url = normalize_candidate_url(final_url)
                visited_pages = {initial_url} if initial_url else set()
                page_queue: deque[str] = deque()

                def process_page(page_html: str, page_url: str) -> None:
                    pagination_urls = parse_pagination(
                        page_html, page_url, self.policy
                    )
                    pagination_set = set(pagination_urls)
                    self._append_candidates(
                        result,
                        seen,
                        [
                            item
                            for item in parse_result_candidates(
                                page_html,
                                page_url,
                                self.policy,
                                spec.source,
                                keyword,
                            )
                            if item.url not in pagination_set
                        ],
                    )
                    for pagination_url in pagination_urls:
                        normalized = normalize_candidate_url(pagination_url)
                        if normalized and normalized not in visited_pages:
                            visited_pages.add(normalized)
                            page_queue.append(normalized)

                process_page(html, final_url)
                additional_requests = 0
                while page_queue and additional_requests < 500:
                    page_url = page_queue.popleft()
                    additional_requests += 1
                    page = await self.get_text(
                        page_url,
                        limit=500,
                        source=spec.source,
                    )
                    if page is None:
                        continue
                    page_html, page_final_url = page
                    process_page(page_html, page_final_url)
        return result

    @staticmethod
    def _append_candidates(
        result: list[Candidate],
        seen: set[str],
        candidates: list[Candidate],
    ) -> None:
        for item in candidates:
            if item.url not in seen:
                seen.add(item.url)
                result.append(item)


class SitemapProvider(Provider):
    source = "sitemap"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        origin: str,
    ):
        super().__init__(client, budget, stats, policy)
        self.origin = origin.rstrip("/")

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        loaded = await self.get_text(
            self.origin + "/sitemap.xml",
            limit=100,
            counts_as_html=False,
        )
        if loaded is None:
            return []
        body, _final_url = loaded
        child_urls = parse_sitemap_index(body, self.policy)
        urls = [] if child_urls else parse_sitemap(body, self.policy)
        for child_url in child_urls:
            child = await self.get_text(
                child_url,
                limit=100,
                counts_as_html=False,
            )
            if child is not None:
                child_body, _child_final_url = child
                urls.extend(parse_sitemap(child_body, self.policy))
        return [
            Candidate(url, self.source, score=55)
            for url in dict.fromkeys(urls)
        ]


class FeedProvider(Provider):
    source = "feed"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        feed_urls: list[str],
    ):
        super().__init__(client, budget, stats, policy)
        self.feed_urls = feed_urls

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        for url in self.feed_urls:
            loaded = await self.get_text(
                url,
                limit=100,
                counts_as_html=False,
            )
            if loaded is None:
                continue
            body, _final_url = loaded
            for item in parse_feed(body, self.policy):
                if item.url not in seen:
                    seen.add(item.url)
                    result.append(item)
        return result


class CategoryProvider(Provider):
    source = "category"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        category_urls: list[str],
        fetcher=None,
        render_mode: str = "auto",
    ):
        super().__init__(client, budget, stats, policy)
        self.category_urls = category_urls
        self.fetcher = fetcher
        self.render_mode = render_mode

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        visited_pages: set[str] = set()
        additional_requests = 0
        keyword = keywords[0] if keywords else ""
        for url in self.category_urls:
            normalized_root = normalize_candidate_url(url)
            if not normalized_root or normalized_root in visited_pages:
                continue
            visited_pages.add(normalized_root)
            loaded = await self.get_text(url, limit=500)
            if loaded is None:
                continue
            body, final_url = loaded
            normalized_final = normalize_candidate_url(final_url)
            if normalized_final:
                visited_pages.add(normalized_final)
            page_queue: deque[str] = deque()
            queued_pages: set[str] = set()

            def process_page(page_body: str, page_url: str) -> list[Candidate]:
                pagination_urls = parse_pagination(
                    page_body, page_url, self.policy
                )
                pagination_set = set(pagination_urls)
                batch = [
                    item
                    for item in parse_result_candidates(
                        page_body,
                        page_url,
                        self.policy,
                        self.source,
                        keyword,
                    )
                    if item.url not in pagination_set
                ]
                self._append_candidates(result, seen, batch)
                for pagination_url in pagination_urls:
                    normalized = normalize_candidate_url(pagination_url)
                    if (
                        normalized
                        and normalized not in visited_pages
                        and normalized not in queued_pages
                    ):
                        queued_pages.add(normalized)
                        page_queue.append(normalized)
                return batch

            batch = process_page(body, final_url)
            if (
                (not batch or looks_js_driven(body))
                and self.render_mode != "off"
                and self.fetcher is not None
            ):
                fetch_rendered_page = getattr(
                    self.fetcher, "fetch_rendered_page", None
                )
                if fetch_rendered_page is not None:
                    rendered_page = await fetch_rendered_page(final_url)
                    rendered = (
                        None
                        if rendered_page is None
                        else (
                            rendered_page.html,
                            rendered_page.links,
                            rendered_page.final_url,
                        )
                    )
                else:
                    legacy = await self.fetcher.fetch_rendered(final_url)
                    rendered = (
                        None
                        if legacy is None
                        else (legacy[0], legacy[1], final_url)
                    )
                if rendered is not None:
                    rendered_html, links, rendered_final_url = rendered
                    excluded_rendered_urls = set(
                        parse_pagination(
                            rendered_html,
                            rendered_final_url,
                            self.policy,
                        )
                    )
                    excluded_rendered_urls.update(
                        filter(
                            None,
                            (
                                normalize_candidate_url(
                                    rendered_final_url
                                ),
                                normalize_candidate_url(final_url),
                                normalize_candidate_url(url),
                            ),
                        )
                    )
                    rendered_candidates = []
                    for link in links:
                        normalized = normalize_candidate_url(
                            urljoin(rendered_final_url, link)
                        )
                        if (
                            normalized
                            and normalized not in excluded_rendered_urls
                            and url_allowed(normalized, self.policy)
                        ):
                            rendered_candidates.append(
                                Candidate(
                                    normalized,
                                    self.source,
                                    keyword=keyword,
                                    score=60,
                                )
                            )
                    self._append_candidates(
                        result, seen, rendered_candidates
                    )
            while page_queue and additional_requests < 500:
                page_url = page_queue.popleft()
                queued_pages.discard(page_url)
                if page_url in visited_pages:
                    continue
                visited_pages.add(page_url)
                additional_requests += 1
                page = await self.get_text(page_url, limit=500)
                if page is None:
                    continue
                page_body, page_final_url = page
                process_page(page_body, page_final_url)
        return result

    @staticmethod
    def _append_candidates(
        result: list[Candidate],
        seen: set[str],
        candidates: list[Candidate],
    ) -> None:
        for item in candidates:
            if item.url not in seen:
                seen.add(item.url)
                result.append(item)


async def _warm_freecms_session(
    client: httpx.AsyncClient,
    budget: BudgetManager,
    stats: DiscoveryStats,
    adapter: FreeCmsAdapter,
    source: str,
) -> None:
    """Visit an HTML page so FreeCMS issues a usable JSESSIONID."""
    remaining = budget.remaining_seconds()
    if remaining <= 0:
        stats.partial = True
        return
    try:
        async with asyncio.timeout(remaining):
            await client.get(adapter.origin + "/")
    except TimeoutError:
        stats.partial = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        stats.warnings.append(
            f"{source}: session warm {type(exc).__name__}"
        )


class FreeCmsApiProvider(Provider):
    source = "site-search-api"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        adapter: FreeCmsAdapter,
        *,
        today: date | None = None,
        max_pages_per_keyword: int = 100,
    ):
        super().__init__(client, budget, stats, policy)
        self.adapter = adapter
        self.today = today or date.today()
        self.max_pages_per_keyword = max_pages_per_keyword

    async def _warm_session(self) -> None:
        await _warm_freecms_session(
            self.client,
            self.budget,
            self.stats,
            self.adapter,
            self.source,
        )

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        source_was_successful = self.source in self.stats.sources_succeeded
        business_success = False
        spec = self.adapter.search_specs()[0]
        selected_keywords = keywords[:6]
        request_limit = (
            len(selected_keywords) * self.max_pages_per_keyword
        )
        cutoff = self.today - timedelta(days=30)
        await self._warm_session()
        if self.budget.expired():
            self.stats.partial = True
            self.stats.note_stop("time-budget")

        for keyword in selected_keywords:
            dates_nonincreasing = True
            last_valid_date: date | None = None
            for page in range(1, self.max_pages_per_keyword + 1):
                if self.budget.expired():
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                    break
                loaded = await self.get_text(
                    spec.url,
                    limit=request_limit,
                    params=spec.params_for(keyword, page),
                    counts_as_html=False,
                )
                if loaded is None:
                    if self.budget.expired():
                        self.stats.partial = True
                        self.stats.note_stop("time-budget")
                    break
                body, _final_url = loaded
                ok, rows, warning = self.adapter.parse_api_response(body)
                if not ok:
                    self.stats.warnings.append(
                        f"{self.source}: {warning}"
                    )
                    break
                business_success = True
                if not rows:
                    break

                parsed_rows: list[tuple[dict, date | None]] = []
                page_has_expired_date = False
                for row in rows:
                    raw_date = row.get("addtimeStr")
                    published: date | None = None
                    if raw_date is not None:
                        try:
                            published = date.fromisoformat(
                                str(raw_date)[:10]
                            )
                        except ValueError:
                            pass
                    parsed_rows.append((row, published))
                    if published is not None:
                        if (
                            last_valid_date is not None
                            and published > last_valid_date
                        ):
                            dates_nonincreasing = False
                        last_valid_date = published
                        if published < cutoff:
                            page_has_expired_date = True

                for row, published in parsed_rows:
                    if published is not None and published < cutoff:
                        continue
                    raw = str(row.get("pageUrl") or "")
                    if not raw:
                        continue
                    candidate_url = urljoin(
                        self.adapter.origin + "/", raw
                    )
                    item_id = row.get("id")
                    query_keys = {
                        key
                        for key, _value in parse_qsl(
                            urlsplit(candidate_url).query,
                            keep_blank_values=True,
                        )
                    }
                    if item_id and "id" not in query_keys:
                        parts = urlsplit(candidate_url)
                        query = parse_qsl(
                            parts.query, keep_blank_values=True
                        )
                        query.append(("id", str(item_id)))
                        candidate_url = urlunsplit(
                            parts._replace(
                                query=urlencode(query, doseq=True)
                            )
                        )
                    candidate_url = normalize_candidate_url(candidate_url)
                    if (
                        not candidate_url
                        or candidate_url in seen
                        or not url_allowed(candidate_url, self.policy)
                    ):
                        continue
                    seen.add(candidate_url)
                    if published is None:
                        self.stats.unknown_date_candidates += 1
                    title = str(row.get("title") or "")
                    result.append(
                        Candidate(
                            candidate_url,
                            self.source,
                            keyword,
                            title,
                            100
                            if keyword.lower() in title.lower()
                            else 75,
                            published_date=(
                                published.isoformat()
                                if published is not None
                                else None
                            ),
                            source_evidence=(self.source,),
                        )
                        )

                await asyncio.sleep(_provider_delay_seconds())
                if page == self.max_pages_per_keyword:
                    self.stats.note_stop("provider-page-limit")
                if dates_nonincreasing and page_has_expired_date:
                    self.stats.note_stop("date-boundary")
                    break
        if source_was_successful or business_success:
            self.stats.sources_succeeded.add(self.source)
        else:
            self.stats.sources_succeeded.discard(self.source)
        if not business_success:
            if self.budget.expired():
                self.stats.partial = True
                self.stats.note_stop("time-budget")
            else:
                self.stats.note_stop("channel-failure")
        return result


class FreeCmsRecentProvider(Provider):
    source = "freecms-recent"
    browser_budget_source = "freecms-recent-browser"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        adapter: FreeCmsAdapter,
        *,
        today: date | None = None,
        max_pages: int = 500,
        browser_loader_factory=None,
    ):
        super().__init__(client, budget, stats, policy)
        self.adapter = adapter
        self.today = today
        self.max_pages = max_pages
        if browser_loader_factory is None:
            from discovery.freecms_browser import freecms_browser_loader

            browser_loader_factory = freecms_browser_loader
        self.browser_loader_factory = browser_loader_factory

    async def _crawl_recent(
        self,
        spec: SearchSpec,
        load_page: Callable[
            [int], Awaitable[tuple[str, str] | None]
        ],
    ) -> tuple[list[Candidate], bool, bool]:
        cutoff = (self.today or date.today()) - timedelta(days=30)
        result: list[Candidate] = []
        seen: set[str] = set()
        any_business_success = False
        clean_completion = False
        dates_nonincreasing = True
        last_valid_date: date | None = None

        for page in range(1, self.max_pages + 1):
            if self.budget.expired():
                self.stats.partial = True
                self.stats.note_stop("time-budget")
                break
            await asyncio.sleep(_provider_delay_seconds())
            loaded = await load_page(page)
            if loaded is None:
                if self.budget.expired():
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                break

            body, _final_url = loaded
            ok, rows, warning = self.adapter.parse_recent_response(body)
            if not ok:
                self.stats.warnings.append(f"{self.source}: {warning}")
                break

            any_business_success = True
            if not rows:
                clean_completion = True
                break

            parsed_rows: list[tuple[dict, date | None]] = []
            page_has_expired_date = False
            for row in rows:
                raw_date = row.get("addtimeStr")
                published: date | None = None
                if raw_date is not None:
                    try:
                        published = date.fromisoformat(
                            str(raw_date)[:10]
                        )
                    except ValueError:
                        pass
                parsed_rows.append((row, published))
                if published is not None:
                    if (
                        last_valid_date is not None
                        and published > last_valid_date
                    ):
                        dates_nonincreasing = False
                    last_valid_date = published
                    if published < cutoff:
                        page_has_expired_date = True

            for row, published in parsed_rows:
                if published is not None and published < cutoff:
                    continue
                raw_url = str(row.get("pageUrl") or "")
                if not raw_url:
                    continue
                try:
                    candidate_url = urljoin(
                        self.adapter.origin + "/", raw_url
                    )
                except (UnicodeError, ValueError):
                    continue
                candidate_url = normalize_candidate_url(candidate_url)
                if (
                    not candidate_url
                    or candidate_url in seen
                    or not url_allowed(candidate_url, self.policy)
                ):
                    continue
                seen.add(candidate_url)
                if published is None:
                    self.stats.unknown_date_candidates += 1
                result.append(
                    Candidate(
                        candidate_url,
                        self.source,
                        title_hint=str(row.get("title") or ""),
                        score=75,
                        published_date=(
                            published.isoformat()
                            if published is not None
                            else None
                        ),
                        source_evidence=(self.source,),
                    )
                )

            if page == self.max_pages:
                self.stats.note_stop("provider-page-limit")
                clean_completion = True
            if dates_nonincreasing and page_has_expired_date:
                self.stats.note_stop("date-boundary")
                clean_completion = True
                break

        return result, any_business_success, clean_completion

    def _reserve_browser_navigation_budget(self) -> bool:
        if self.budget.expired():
            self.stats.partial = True
            self.stats.note_stop("time-budget")
            return False
        request_limit = self.max_pages + 2
        provider_used = self.budget.provider_requests.get(
            self.browser_budget_source, 0
        )
        available = self.budget.page_limit - self.budget.used_html_pages
        if (
            available < 2
            and self.budget.expand(high_value_remaining=1)
        ):
            self.stats.budget_expanded = True
            available = (
                self.budget.page_limit - self.budget.used_html_pages
            )
        if available < 2:
            self.stats.partial = True
            self.stats.note_stop("html-page-budget")
            return False
        if provider_used + 2 > request_limit:
            self.stats.note_stop("provider-page-limit")
            return False
        reserved_provider = (
            self.budget.reserve_provider(
                self.browser_budget_source, request_limit
            )
            and self.budget.reserve_provider(
                self.browser_budget_source, request_limit
            )
        )
        if not reserved_provider:
            if self.budget.expired():
                self.stats.partial = True
                self.stats.note_stop("time-budget")
            else:
                self.stats.note_stop("provider-page-limit")
            return False
        return self.budget.reserve_html() and self.budget.reserve_html()

    async def _crawl_browser(
        self,
        spec: SearchSpec,
    ) -> tuple[list[Candidate], bool, bool]:
        if not self._reserve_browser_navigation_budget():
            return [], False, False

        browser_failed = False

        async def load_page(page: int):
            nonlocal browser_failed
            if self.budget.expired():
                self.stats.partial = True
                self.stats.note_stop("time-budget")
                return None
            if not self.budget.reserve_provider(
                self.browser_budget_source,
                self.max_pages + 2,
            ):
                if self.budget.expired():
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                return None
            remaining = self.budget.remaining_seconds()
            try:
                async with asyncio.timeout(remaining):
                    loaded = await loader.load(spec, page)
            except TimeoutError:
                self.stats.partial = True
                self.stats.note_stop("time-budget")
                return None
            except asyncio.CancelledError:
                raise
            except Exception:
                loaded = None
            if loaded is None:
                browser_failed = True
            return loaded

        result: list[Candidate] = []
        any_business_success = False
        clean_completion = False
        try:
            remaining = self.budget.remaining_seconds()
            factory_parameters = inspect.signature(
                self.browser_loader_factory
            ).parameters.values()
            supports_policy = any(
                parameter.name == "policy"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in factory_parameters
            )
            loader_context = (
                self.browser_loader_factory(
                    self.adapter.origin,
                    policy=self.policy,
                )
                if supports_policy
                else self.browser_loader_factory(self.adapter.origin)
            )
            async with asyncio.timeout(remaining):
                async with loader_context as loader:
                    self.stats.rendered_pages += 2
                    (
                        result,
                        any_business_success,
                        clean_completion,
                    ) = await self._crawl_recent(spec, load_page)
        except TimeoutError:
            self.stats.partial = True
            self.stats.note_stop("time-budget")
        except asyncio.CancelledError:
            raise
        except Exception:
            browser_failed = True
        if not clean_completion and browser_failed:
            self.stats.warnings.append(
                f"{self.source}: browser fallback failed"
            )
        return result, any_business_success, clean_completion

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        spec = self.adapter.recent_notice_spec()
        if spec is None:
            return []

        self.stats.recent_window_days = 30
        await _warm_freecms_session(
            self.client,
            self.budget,
            self.stats,
            self.adapter,
            self.source,
        )
        if self.budget.expired():
            self.stats.partial = True
            self.stats.note_stop("time-budget")
            return []

        async def load_static(page: int):
            return await self.get_text(
                spec.url,
                limit=self.max_pages,
                params=spec.params_for("", page),
                counts_as_html=False,
            )

        (
            result,
            any_business_success,
            clean_completion,
        ) = await self._crawl_recent(spec, load_static)
        if not any_business_success and not self.budget.expired():
            (
                result,
                any_business_success,
                clean_completion,
            ) = await self._crawl_browser(spec)

        if any_business_success:
            self.stats.sources_succeeded.add(self.source)
        else:
            self.stats.sources_succeeded.discard(self.source)
        if not clean_completion:
            self.stats.partial = True
            if self.budget.expired():
                self.stats.note_stop("time-budget")
            else:
                self.stats.note_stop("channel-failure")
        return result


def _parse_search_count(body: str) -> int | None:
    """解析 searchClassCount.aspx 的响应。

    真实接口返回 JSON：{"code":0, "data":[{"name":"人事任免","Count":2}, ...]}，
    总数取各栏目 Count 之和；兼容个别站点直接返回纯数字的情况。
    无法识别时返回 None。
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        if str(data.get("code")) != "0":
            return None
        rows = data.get("data")
        if not isinstance(rows, list):
            return None
        total = 0
        for row in rows:
            if not isinstance(row, dict):
                return None
            try:
                total += int(row.get("Count") or 0)
            except (TypeError, ValueError):
                return None
        return total
    if len(body) <= 200:
        match = re.search(r"(?<![-\d])\d+", body)
        if match:
            return int(match.group())
    return None


class YunnanCmsProvider(Provider):
    source = "site-search"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        adapter: YunnanCmsAdapter,
    ):
        super().__init__(client, budget, stats, policy)
        self.adapter = adapter

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        source_was_successful = self.source in self.stats.sources_succeeded
        page_success = False
        for keyword_index, keyword in enumerate(keywords[:6]):
            count = await self.get_text(
                self.adapter.origin + "/searchClassCount.aspx",
                limit=1,
                params={"tags": keyword},
                counts_as_html=False,
                budget_source=f"{self.source}-count-{keyword_index}",
            )
            if count is None:
                continue
            count_body, _count_final_url = count
            total = _parse_search_count(count_body.strip())
            if total is None:
                self.stats.warnings.append(
                    f"{self.source}: invalid count"
                )
                continue
            if total == 0:
                continue
            for page_no in range(1, 21):
                await asyncio.sleep(_provider_delay_seconds())
                loaded = await self.get_text(
                    self.adapter.origin + "/searchN.aspx",
                    limit=10,
                    params={
                        "page": page_no,
                        "type": "",
                        "tags": keyword,
                    },
                )
                if loaded is None:
                    break
                page_success = True
                body, final_url = loaded
                batch = parse_result_candidates(
                    body,
                    final_url,
                    self.policy,
                    self.source,
                    keyword,
                )
                for item in batch:
                    if item.url not in seen:
                        seen.add(item.url)
                        result.append(item)
                if not batch:
                    break
        if source_was_successful or page_success:
            self.stats.sources_succeeded.add(self.source)
        else:
            self.stats.sources_succeeded.discard(self.source)
        return result


class YngpProvider(Provider):
    """云南省政府采购网采购公告接口（bootgrid AJAX）。

    服务端要求先按浏览器加载列表页的顺序建立会话（取列表页 → 调行政区
    划接口），否则公告接口只返回空 data；第 2 页起和 rowCount≠10 的请
    求需要滑块验证码，因此每个时间窗只取第一页（最新 10 条），用
    query_startTime/query_endTime 切窗代替翻页：窗口 total ≤ 10 时第
    一页即全部，total > 10 时对半细分。不传 query_type 时服务端只返
    回采购意向（默认 sign=23），所以逐类别查询。
    """

    source = "yngp-api"

    # 列表页核心栏目：采购意向公开、招标/预审/谈判/磋商/询价公告
    DEFAULT_QUERY_TYPES = ("23", "1")

    # 类别关键词 → query_type 映射。当用户搜索的关键词恰好是类别名称时，
    # 对该类别不应再用该关键词过滤标题，应拉取全部近 N 天公告由正文匹配筛选。
    CATEGORY_KEYWORD_MAP: dict[str, str] = {
        "采购意向公开": "23",
        "采购意向": "23",
        "意向公开": "23",
        "招标公告": "1",
        "招标": "1",
        "单一来源公示": "3",
        "单一来源": "3",
        "结果公告": "2",
        "更正公告": "4",
        "废标公告": "5",
        "合同公告": "8",
        "验收公告": "7",
    }

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        adapter: YngpAdapter,
        *,
        today: date | None = None,
        query_types: tuple[str, ...] = DEFAULT_QUERY_TYPES,
        recent_days: int = 30,
        max_windows_per_query: int = 60,
        full_sweep: bool = True,
    ):
        super().__init__(client, budget, stats, policy)
        self.adapter = adapter
        self.today = today or date.today()
        self.query_types = query_types
        self.recent_days = recent_days
        # 单个（关键字 × 类别）组合允许的最大时间窗请求数
        self.max_windows_per_query = max_windows_per_query
        self.full_sweep = full_sweep

    def _headers(self) -> dict[str, str]:
        return {
            "Origin": self.adapter.origin,
            "Referer": self.adapter.list_url,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": (
                "application/x-www-form-urlencoded; charset=UTF-8"
            ),
        }

    async def _warm_session(self) -> None:
        """复刻浏览器打开列表页时的请求序列，建立可用会话。"""
        remaining = self.budget.remaining_seconds()
        if remaining <= 0:
            self.stats.partial = True
            return
        try:
            async with asyncio.timeout(remaining):
                await self.client.get(
                    self.adapter.list_url,
                    headers={"Referer": self.adapter.origin + "/"},
                )
                await self.client.post(
                    self.adapter.warm_url,
                    content="",
                    headers=self._headers(),
                )
        except TimeoutError:
            self.stats.partial = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.warnings.append(
                f"{self.source}: session warm {type(exc).__name__}"
            )

    async def _post_api(
        self,
        keyword: str,
        query_type: str,
        start: date,
        end: date,
        limit: int,
    ) -> str | None:
        self.stats.sources_tried.add(self.source)
        last_error: Exception | None = None
        # 站点偶尔重置连接，失败时重试一次
        for _attempt in range(2):
            remaining = self.budget.remaining_seconds()
            if remaining <= 0:
                self.stats.partial = True
                return None
            if not self.budget.reserve_provider(self.source, limit):
                return None
            try:
                async with asyncio.timeout(remaining):
                    response = await self.client.post(
                        self.adapter.api_url,
                        params={"captchaCheckFlag": "0", "p": 1},
                        data={
                            "current": 1,
                            "rowCount": 10,
                            "searchPhrase": "",
                            "query_bulletintitle": keyword,
                            "query_type": query_type,
                            "query_startTime": start.isoformat(),
                            "query_endTime": end.isoformat(),
                        },
                        headers=self._headers(),
                    )
            except TimeoutError:
                self.stats.partial = True
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                continue
            try:
                response.raise_for_status()
            except Exception as exc:
                self.stats.warnings.append(
                    f"{self.source}: {type(exc).__name__}"
                )
                return None
            self.stats.sources_succeeded.add(self.source)
            return response.text
        self.stats.warnings.append(
            f"{self.source}: {type(last_error).__name__}"
        )
        return None

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        source_was_successful = (
            self.source in self.stats.sources_succeeded
        )
        business_success = False
        selected_keywords = keywords[:6]
        request_limit = (
            (len(selected_keywords) + 1)
            * len(self.query_types)
            * self.max_windows_per_query
        )
        await self._warm_session()
        if self.budget.expired():
            self.stats.partial = True
            self.stats.note_stop("time-budget")
            return result

        # 至少保留 8 秒给候选页抓取，其余时间全力搜索
        _FETCH_RESERVE_SECONDS = 8

        # 记录哪些 query_type 已经被某个类别关键词"全量拉取"过，
        # 后续无关键词全量补拉时跳过，避免重复
        unfiltered_types_covered: set[str] = set()

        for keyword in selected_keywords:
            for query_type in self.query_types:
                if self.budget.expired():
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                    break
                if self.budget.remaining_seconds() < _FETCH_RESERVE_SECONDS:
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                    break
                window_success = await self._collect_recent(
                    keyword, query_type, request_limit, result, seen
                )
                business_success = business_success or window_success
                # 如果该关键词是该类别的类别名，_collect_recent 已用
                # effective_keyword="" 全量拉取，标记跳过
                if self.CATEGORY_KEYWORD_MAP.get(keyword) == query_type:
                    unfiltered_types_covered.add(query_type)

        # 全量补拉：对未被类别关键词覆盖的 query_type，
        # 以关键词="" 拉取全部近期公告，交给正文匹配发现标题外命中
        # 先重新预热会话，避免长时间关键词筛选后会话过期
        if self.full_sweep:
            await self._warm_session()
            for query_type in self.query_types:
                if query_type in unfiltered_types_covered:
                    continue
                if self.budget.expired():
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                    break
                if self.budget.remaining_seconds() < _FETCH_RESERVE_SECONDS:
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                    break
                window_success = await self._collect_recent(
                    "", query_type, request_limit, result, seen
                )
                business_success = business_success or window_success

        if source_was_successful or business_success:
            self.stats.sources_succeeded.add(self.source)
        else:
            self.stats.sources_succeeded.discard(self.source)
        if not business_success:
            if self.budget.expired():
                self.stats.partial = True
                self.stats.note_stop("time-budget")
            else:
                self.stats.note_stop("channel-failure")
        return result

    async def _collect_recent(
        self,
        keyword: str,
        query_type: str,
        limit: int,
        result: list[Candidate],
        seen: set[str],
    ) -> bool:
        """用时间窗分片代替翻页，收集最近 recent_days 天的公告。

        接口第 2 页起要滑块验证码，但时间范围过滤不受限：窗口
        total ≤ 10 时第一页就是全部；total > 10 时对半细分直到每片
        都能被一页装下。优先处理较新的窗口。
        """
        business_success = False
        cutoff = self.today - timedelta(days=self.recent_days)
        stack = [(cutoff, self.today)]
        queries = 0
        overflow_warned = False
        # 余量不足时提前退出，给候选页抓取留出时间
        _FETCH_RESERVE_SECONDS = 5
        while stack:
            if self.budget.expired():
                self.stats.partial = True
                self.stats.note_stop("time-budget")
                break
            if queries >= self.max_windows_per_query:
                self.stats.note_stop("provider-page-limit")
                break
            if self.budget.remaining_seconds() < _FETCH_RESERVE_SECONDS:
                self.stats.partial = True
                self.stats.note_stop("time-budget")
                break
            win_start, win_end = stack.pop()
            queries += 1
            # 当关键词本身是类别名时，对该类别不按标题过滤，
            # 拉取全部近 N 天公告，交给正文匹配阶段筛选。
            effective_keyword = (
                ""
                if self.CATEGORY_KEYWORD_MAP.get(keyword) == query_type
                else keyword
            )
            body = await self._post_api(
                effective_keyword, query_type, win_start, win_end, limit
            )
            if body is None:
                if self.budget.expired():
                    self.stats.partial = True
                    self.stats.note_stop("time-budget")
                continue
            ok, total, rows, warning = self.adapter.parse_api_response(
                body
            )
            if not ok:
                self.stats.warnings.append(f"{self.source}: {warning}")
                continue
            business_success = True
            if (
                total is not None
                and total > len(rows)
                and win_start < win_end
            ):
                mid = win_start + (win_end - win_start) // 2
                # 栈后进先出：较新的半边先处理
                stack.append((win_start, mid))
                stack.append((mid + timedelta(days=1), win_end))
                continue
            if (
                total is not None
                and total > len(rows)
                and not overflow_warned
            ):
                overflow_warned = True
                self.stats.warnings.append(
                    f"{self.source}: 单日公告超过 10 条，"
                    "超出部分未获取"
                )
            self._append_rows(rows, keyword, cutoff, result, seen)
        return business_success

    def _append_rows(
        self,
        rows: list[dict],
        keyword: str,
        cutoff: date,
        result: list[Candidate],
        seen: set[str],
    ) -> None:
        for row in rows:
            raw_date = row.get("finishday")
            published: date | None = None
            if raw_date is not None:
                try:
                    published = date.fromisoformat(str(raw_date)[:10])
                except ValueError:
                    pass
            if published is not None and published < cutoff:
                continue
            raw_url = self.adapter.detail_url(row)
            if not raw_url:
                continue
            candidate_url = normalize_candidate_url(raw_url)
            if (
                not candidate_url
                or candidate_url in seen
                or not url_allowed(candidate_url, self.policy)
            ):
                continue
            seen.add(candidate_url)
            if published is None:
                self.stats.unknown_date_candidates += 1
            title = str(row.get("bulletintitle") or "")
            district = str(row.get("districtname") or "")
            result.append(
                Candidate(
                    candidate_url,
                    self.source,
                    keyword,
                    title,
                    100 if keyword.lower() in title.lower() else 75,
                    published_date=(
                        published.isoformat()
                        if published is not None
                        else None
                    ),
                    source_evidence=(self.source,),
                    district=district,
                )
            )
