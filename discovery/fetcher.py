from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

from crawler import PAGE_TIMEOUT, USER_AGENT, fetch_html_retry
from discovery.models import BudgetManager, DiscoveryStats


class DiscoveryFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        concurrency: int = 8,
        per_host_concurrency: int = 4,
    ):
        self.client = client
        self.budget = budget
        self.stats = stats
        self.semaphore = asyncio.Semaphore(concurrency)
        self.per_host_concurrency = per_host_concurrency
        self.host_semaphores: dict[str, asyncio.Semaphore] = {}

    def host_semaphore(self, url: str) -> asyncio.Semaphore:
        try:
            host = urlsplit(url).netloc.lower()
        except ValueError:
            host = url.lower()
        return self.host_semaphores.setdefault(
            host, asyncio.Semaphore(self.per_host_concurrency)
        )

    def _reserve(self) -> bool:
        if self.budget.reserve_html():
            return True
        self.stats.partial = True
        return False

    async def fetch_html(self, url: str) -> str | None:
        if not self._reserve():
            return None
        async with self.semaphore, self.host_semaphore(url):
            try:
                return await fetch_html_retry(
                    self.client, url, attempts=2, base_delay=1.0
                )
            except ValueError:
                self.stats.warnings.append(f"{url}: 非 HTML 内容")
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                self.stats.warnings.append(f"{url}: {type(exc).__name__}")
        return None

    async def fetch_rendered(
        self, url: str
    ) -> tuple[str, list[str]] | None:
        if not self._reserve():
            return None
        async with self.semaphore, self.host_semaphore(url):
            try:
                import renderer

                html, links = await renderer.render_page(url)
            except Exception as exc:
                self.stats.warnings.append(
                    f"{url}: render {type(exc).__name__}"
                )
                return None
        self.stats.rendered_pages += 1
        return html, links


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
