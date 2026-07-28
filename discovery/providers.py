from __future__ import annotations

from urllib.parse import urlencode, urljoin

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
        if not self.budget.reserve_provider(budget_source or source, limit):
            return None
        if counts_as_html and not self.budget.reserve_html():
            self.stats.partial = True
            return None
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
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
                self._append_candidates(
                    result,
                    seen,
                    parse_result_candidates(
                        html,
                        final_url,
                        self.policy,
                        spec.source,
                        keyword,
                    ),
                )
                for page_url in parse_pagination(
                    html, final_url, self.policy
                )[:9]:
                    page = await self.get_text(
                        page_url,
                        limit=10,
                        source=spec.source,
                    )
                    if page is None:
                        continue
                    page_html, page_final_url = page
                    self._append_candidates(
                        result,
                        seen,
                        parse_result_candidates(
                            page_html,
                            page_final_url,
                            self.policy,
                            spec.source,
                            keyword,
                        ),
                    )
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
        keyword = keywords[0] if keywords else ""
        for url in self.category_urls[:8]:
            loaded = await self.get_text(url, limit=20)
            if loaded is None:
                continue
            body, final_url = loaded
            page_urls = parse_pagination(body, final_url, self.policy)[:12]
            page_url_set = set(page_urls)
            batch = parse_result_candidates(
                body,
                final_url,
                self.policy,
                self.source,
                keyword,
            )
            batch = [item for item in batch if item.url not in page_url_set]
            self._append_candidates(result, seen, batch)
            if (
                (not batch or looks_js_driven(body))
                and self.render_mode != "off"
                and self.fetcher is not None
            ):
                rendered = await self.fetcher.fetch_rendered(final_url)
                if rendered is not None:
                    _rendered_html, links = rendered
                    rendered_candidates = []
                    for link in links:
                        normalized = normalize_candidate_url(
                            urljoin(final_url, link)
                        )
                        if normalized and url_allowed(
                            normalized, self.policy
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
            for page_url in page_urls:
                page = await self.get_text(page_url, limit=20)
                if page is None:
                    continue
                page_body, page_final_url = page
                self._append_candidates(
                    result,
                    seen,
                    parse_result_candidates(
                        page_body,
                        page_final_url,
                        self.policy,
                        self.source,
                        keyword,
                    ),
                )
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
                self.stats.sources_succeeded.discard(self.source)
                self.stats.warnings.append(f"{self.source}: {warning}")
                continue
            for row in rows:
                raw = str(row.get("pageUrl") or "")
                if not raw:
                    continue
                candidate_url = urljoin(self.adapter.origin + "/", raw)
                item_id = row.get("id")
                if item_id and "id=" not in candidate_url:
                    separator = "&" if "?" in candidate_url else "?"
                    candidate_url += separator + urlencode({"id": item_id})
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
        return result
