from __future__ import annotations

import asyncio
import re
from collections import deque
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import httpx

from discovery.adapters import FreeCmsAdapter, YunnanCmsAdapter
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

    async def get_text(
        self,
        url: str,
        *,
        limit: int = 10,
        params: dict | None = None,
        counts_as_html: bool = True,
        source: str | None = None,
        budget_source: str | None = None,
    ) -> tuple[str, str] | None:
        source = source or self.source
        self.stats.sources_tried.add(source)
        budget_key = budget_source or source
        provider_used = self.budget.provider_requests.get(budget_key, 0)
        if self.budget.expired() or provider_used >= limit:
            return None
        if (
            counts_as_html
            and self.budget.used_html_pages >= self.budget.page_limit
        ):
            self.stats.partial = True
            return None
        if not self.budget.reserve_provider(budget_key, limit):
            return None
        if counts_as_html and not self.budget.reserve_html():
            if provider_used:
                self.budget.provider_requests[budget_key] = provider_used
            else:
                self.budget.provider_requests.pop(budget_key, None)
            self.stats.partial = True
            return None
        try:
            remaining = self.budget.remaining_seconds()
            if remaining <= 0:
                self.stats.partial = True
                return None
            async with asyncio.timeout(remaining):
                response = await self.client.get(url, params=params)
                response.raise_for_status()
        except TimeoutError:
            self.stats.partial = True
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.warnings.append(f"{source}: {type(exc).__name__}")
            return None
        self.stats.sources_succeeded.add(source)
        return response.text, str(response.url)


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
                    limit=10,
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
                while page_queue and additional_requests < 9:
                    page_url = page_queue.popleft()
                    additional_requests += 1
                    page = await self.get_text(
                        page_url,
                        limit=10,
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
            limit=5,
            counts_as_html=False,
        )
        if loaded is None:
            return []
        body, _final_url = loaded
        child_urls = parse_sitemap_index(body, self.policy)[:4]
        urls = [] if child_urls else parse_sitemap(body, self.policy)
        for child_url in child_urls:
            child = await self.get_text(
                child_url,
                limit=5,
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
        for url in self.feed_urls[:5]:
            loaded = await self.get_text(
                url,
                limit=5,
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
        for url in self.category_urls[:8]:
            normalized_root = normalize_candidate_url(url)
            if not normalized_root or normalized_root in visited_pages:
                continue
            visited_pages.add(normalized_root)
            loaded = await self.get_text(url, limit=20)
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
                rendered = await self.fetcher.fetch_rendered(final_url)
                if rendered is not None:
                    rendered_html, links = rendered
                    excluded_rendered_urls = set(
                        parse_pagination(
                            rendered_html, final_url, self.policy
                        )
                    )
                    excluded_rendered_urls.update(
                        filter(
                            None,
                            (
                                normalize_candidate_url(final_url),
                                normalize_candidate_url(url),
                            ),
                        )
                    )
                    rendered_candidates = []
                    for link in links:
                        normalized = normalize_candidate_url(
                            urljoin(final_url, link)
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
            while page_queue and additional_requests < 12:
                page_url = page_queue.popleft()
                queued_pages.discard(page_url)
                if page_url in visited_pages:
                    continue
                visited_pages.add(page_url)
                additional_requests += 1
                page = await self.get_text(page_url, limit=20)
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


class FreeCmsApiProvider(Provider):
    source = "site-search-api"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
        adapter: FreeCmsAdapter,
    ):
        super().__init__(client, budget, stats, policy)
        self.adapter = adapter

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        source_was_successful = self.source in self.stats.sources_succeeded
        business_success = False
        spec = self.adapter.search_specs()[0]
        for keyword in keywords[:6]:
            loaded = await self.get_text(
                spec.url,
                limit=10,
                params=spec.params_for(keyword),
                counts_as_html=False,
            )
            if loaded is None:
                continue
            body, _final_url = loaded
            ok, rows, warning = self.adapter.parse_api_response(body)
            if not ok:
                self.stats.warnings.append(f"{self.source}: {warning}")
                continue
            business_success = True
            for row in rows:
                raw = str(row.get("pageUrl") or "")
                if not raw:
                    continue
                candidate_url = urljoin(self.adapter.origin + "/", raw)
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
                        parts._replace(query=urlencode(query, doseq=True))
                    )
                candidate_url = normalize_candidate_url(candidate_url)
                if (
                    not candidate_url
                    or candidate_url in seen
                    or not url_allowed(candidate_url, self.policy)
                ):
                    continue
                seen.add(candidate_url)
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
                    )
                )
        if source_was_successful or business_success:
            self.stats.sources_succeeded.add(self.source)
        else:
            self.stats.sources_succeeded.discard(self.source)
        return result


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
            stripped_count = count_body.strip()
            count_match = (
                re.search(r"(?<![-\d])\d+", stripped_count)
                if len(stripped_count) <= 200
                else None
            )
            if count_match is None:
                self.stats.warnings.append(
                    f"{self.source}: invalid count"
                )
                continue
            if int(count_match.group()) == 0:
                continue
            for page_no in range(1, 11):
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
