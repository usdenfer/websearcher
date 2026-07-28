from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit

import httpx

from crawler import PAGE_TIMEOUT, USER_AGENT, fetch_html_retry
from discovery.models import BudgetManager, DiscoveryStats


def _canonical_host(host: str) -> str | None:
    normalized = host.lower().rstrip(".")
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        try:
            return normalized.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return None


def _url_parts(
    url: str,
) -> tuple[str, str, int | None, str] | None:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = _canonical_host(parts.hostname or "")
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

    async def fetch_html(self, url: str) -> str | None:
        if not self._reserve():
            return None
        async with self.semaphore, self.host_semaphore(url):
            try:
                return await fetch_html_retry(
                    self.client, url, attempts=2, base_delay=1.0
                )
            except ValueError:
                self.stats.warnings.append(
                    f"{_sanitize_url(url)}: 非 HTML 内容"
                )
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                self.stats.warnings.append(
                    f"{_sanitize_url(url)}: {type(exc).__name__}"
                )
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
                    f"{_sanitize_url(url)}: render {type(exc).__name__}"
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
