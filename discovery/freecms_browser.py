from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urljoin, urlsplit

from discovery.models import DomainPolicy, SearchSpec
from discovery.urltools import url_allowed

_CATEGORY_PATH = "freecms/site/zygjjgzfcgzx/cggg/index.html"
_FETCH_SCRIPT = """async ({url, headers}) => {
  const response = await fetch(url, {
    method: "GET",
    credentials: "same-origin",
    headers,
  });
  return {text: await response.text(), url: response.url};
}"""
_FETCH_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
}
_SAFE_LOCAL_SCHEMES = {"about", "blob", "data"}


def _request_allowed(url: str, policy: DomainPolicy) -> bool:
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return False
    scheme = parts.scheme.lower()
    if scheme in _SAFE_LOCAL_SCHEMES:
        return True
    if scheme not in {"http", "https"}:
        return False
    origin_url = parts._replace(
        path="/",
        query="",
        fragment="",
    ).geturl()
    return url_allowed(origin_url, policy)


class _FreeCmsBrowserLoader:
    def __init__(self, page, policy: DomainPolicy):
        self._page = page
        self._policy = policy

    async def load(
        self,
        spec: SearchSpec,
        page_no: int,
    ) -> tuple[str, str] | None:
        query = urlencode(spec.params_for("", page_no), safe=",")
        url = f"{spec.url}?{query}"
        try:
            loaded = await self._page.evaluate(
                _FETCH_SCRIPT,
                {"url": url, "headers": dict(_FETCH_HEADERS)},
            )
        except Exception:
            return None
        if not isinstance(loaded, dict):
            return None
        body = loaded.get("text")
        final_url = loaded.get("url")
        if not isinstance(body, str) or not isinstance(final_url, str):
            return None
        if not url_allowed(final_url, self._policy):
            return None
        return body, final_url


@asynccontextmanager
async def freecms_browser_loader(
    origin: str,
    *,
    policy: DomainPolicy,
):
    """Warm FreeCMS in Chromium, then expose same-page recent API loading."""
    import renderer

    root_url = origin.rstrip("/") + "/"
    category_url = urljoin(root_url, _CATEGORY_PATH)
    try:
        async with renderer.browser_page_session() as page:
            async def guard_request(route, request):
                if _request_allowed(request.url, policy):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")

            await page.route("**/*", guard_request)
            for url in (root_url, category_url):
                response = await page.goto(
                    url,
                    timeout=renderer.RENDER_TIMEOUT_MS,
                    wait_until="domcontentloaded",
                )
                if response is not None and response.status >= 400:
                    raise renderer.RenderError("FreeCMS 页面加载失败")
                final_url = (
                    response.url
                    if response is not None
                    else getattr(page, "url", "")
                )
                if not url_allowed(final_url, policy):
                    raise renderer.RenderError("FreeCMS 页面重定向超出范围")
            yield _FreeCmsBrowserLoader(page, policy)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise renderer.RenderError("FreeCMS 浏览器回退初始化失败") from None
