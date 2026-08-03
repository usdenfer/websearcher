from __future__ import annotations

import asyncio
import inspect
import os
from urllib.parse import urlsplit

import httpx

from crawler import (
    FetchedHtml,
    PAGE_TIMEOUT,
    USER_AGENT,
    PageBudgetExhausted,
    UnsafeRedirect,
    fetch_html_retry,
)
from discovery.models import BudgetManager, DiscoveryStats, DomainPolicy
from discovery.urltools import canonical_host, url_allowed


def _url_parts(
    url: str,
) -> tuple[str, str, int | None, str] | None:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = canonical_host(parts.hostname or "")
        port = parts.port
    except (TypeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    return scheme, host, port, parts.path


def _host_key(url: str) -> tuple[str, int | None]:
    parsed = _url_parts(url)
    if parsed is None:
        return "<invalid-url>", None
    scheme, host, port, _path = parsed
    effective_port = port
    if effective_port is None:
        effective_port = 443 if scheme == "https" else 80
    return host, effective_port


def _sanitize_url(url: str) -> str:
    parsed = _url_parts(url)
    if parsed is None:
        return "<invalid-url>"
    scheme, host, port, path = parsed
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{display_host}{port_suffix}{path or '/'}"


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, ""))
    except (ValueError, TypeError):
        return default


class DiscoveryFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        concurrency: int | None = None,
        per_host_concurrency: int | None = None,
        policy: DomainPolicy | None = None,
    ):
        if concurrency is None:
            concurrency = _env_int("DISCOVERY_CONCURRENCY", 6)
        if per_host_concurrency is None:
            per_host_concurrency = _env_int("MAX_PER_HOST_CONCURRENCY", 3)
        self.client = client
        self.budget = budget
        self.stats = stats
        self.semaphore = asyncio.Semaphore(concurrency)
        self.per_host_concurrency = per_host_concurrency
        self.policy = policy
        self._rate_limiter = None
        self.host_semaphores: dict[
            tuple[str, int | None], asyncio.Semaphore
        ] = {}

    def host_semaphore(self, url: str) -> asyncio.Semaphore:
        return self.host_semaphores.setdefault(
            _host_key(url), asyncio.Semaphore(self.per_host_concurrency)
        )

    def _reserve(self) -> bool:
        if self.budget.reserve_html():
            return True
        self.stats.partial = True
        return False

    async def _ensure_rate_limiter(self) -> None:
        if self._rate_limiter is not None:
            return
        from discovery.ratelimit import get_rate_limiter
        self._rate_limiter = await get_rate_limiter()

    def _allowed(self, url: str) -> bool:
        return self.policy is None or url_allowed(url, self.policy)

    async def fetch_html_page(self, url: str) -> FetchedHtml | None:
        if not self._allowed(url):
            return None
        await self._ensure_rate_limiter()
        async with self.semaphore, self.host_semaphore(url):
            try:
                remaining = self.budget.remaining_seconds()
                if remaining <= 0:
                    self.stats.partial = True
                    return None
                await self._rate_limiter.wait(url)
                async with asyncio.timeout(remaining):
                    parameters = inspect.signature(
                        fetch_html_retry
                    ).parameters
                    kwargs = {
                        "attempts": 2,
                        "base_delay": 1.0,
                        "reserve_request": self._reserve,
                    }
                    if "include_final_url" in parameters:
                        kwargs["include_final_url"] = True
                    if (
                        self.policy is not None
                        and "redirect_allowed" in parameters
                    ):
                        kwargs["redirect_allowed"] = self._allowed
                    loaded = await fetch_html_retry(
                        self.client,
                        url,
                        **kwargs,
                    )
                    if loaded is None:
                        return None
                    page = (
                        loaded
                        if isinstance(loaded, FetchedHtml)
                        else FetchedHtml(loaded, url)
                    )
                    if not self._allowed(page.final_url):
                        return None
                    return page
            except TimeoutError:
                self.stats.partial = True
            except PageBudgetExhausted:
                self.stats.partial = True
            except UnsafeRedirect:
                self.stats.warnings.append(
                    f"{_sanitize_url(url)}: 重定向目标不在允许范围"
                )
            except asyncio.CancelledError:
                raise
            except ValueError:
                self.stats.warnings.append(
                    f"{_sanitize_url(url)}: 非 HTML 内容"
                )
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                    await self._rate_limiter.report_rate_limited(url)
                self.stats.warnings.append(
                    f"{_sanitize_url(url)}: {type(exc).__name__}"
                )
        return None

    async def fetch_html(self, url: str) -> str | None:
        page = await self.fetch_html_page(url)
        return None if page is None else page.html

    async def fetch_rendered_page(self, url: str):
        if not self._allowed(url):
            return None
        await self._ensure_rate_limiter()
        async with self.semaphore, self.host_semaphore(url):
            try:
                import renderer
                remaining = self.budget.remaining_seconds()
                if remaining <= 0:
                    self.stats.partial = True
                    return None
                await self._rate_limiter.wait(url)
                async with asyncio.timeout(remaining):
                    if self.policy is not None:
                        result = await renderer.render_page_result(
                            url,
                            navigation_allowed=self._allowed,
                            reserve_request=self._reserve,
                        )
                    else:
                        if (
                            "reserve_request"
                            in inspect.signature(
                                renderer.render_page
                            ).parameters
                        ):
                            html, links = await renderer.render_page(
                                url, reserve_request=self._reserve
                            )
                        else:
                            if not self._reserve():
                                return None
                            html, links = await renderer.render_page(url)
                        result = renderer.RenderedPage(html, links, url)
            except TimeoutError:
                self.stats.partial = True
                return None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.warnings.append(
                    f"{_sanitize_url(url)}: render {type(exc).__name__}"
                )
                return None
        if not self._allowed(result.final_url):
            return None
        self.stats.rendered_pages += 1
        return result

    async def fetch_rendered(
        self, url: str
    ) -> tuple[str, list[str]] | None:
        page = await self.fetch_rendered_page(url)
        if page is None:
            return None
        return page.html, page.links


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
