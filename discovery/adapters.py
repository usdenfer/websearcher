from __future__ import annotations

import json
from urllib.parse import SplitResult, urljoin, urlsplit

from discovery.models import DomainPolicy, SearchSpec


def _safe_urlsplit(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
        _hostname = parts.hostname
        _port = parts.port
    except (UnicodeError, ValueError):
        return None
    return parts


def _origin(start_url: str) -> str:
    parts = _safe_urlsplit(start_url)
    if parts is None or not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc}"


class SiteAdapter:
    profile = "generic"

    def domain_policy(self, start_url: str) -> DomainPolicy:
        parts = _safe_urlsplit(start_url)
        host = (parts.hostname or "").lower().rstrip(".") if parts else ""
        allowed_hosts = frozenset({host}) if host else frozenset()
        return DomainPolicy(host, allowed_hosts)

    def search_specs(self) -> list[SearchSpec]:
        return []

    def category_urls(self) -> list[str]:
        return []


class FreeCmsAdapter(SiteAdapter):
    profile = "freecms"

    def __init__(self, start_url: str):
        self.origin = _origin(start_url)

    def search_specs(self) -> list[SearchSpec]:
        return [
            SearchSpec(
                "site-search-api",
                self.origin + "/freecms/rest/v1/notice/searchAll.do",
                "title",
                "currPage",
                (("pageSize", "10"),),
            )
        ]

    def category_urls(self) -> list[str]:
        base = self.origin + "/freecms/site/zygjjgzfcgzx/"
        return [
            urljoin(base, "cggg/index.html"),
            urljoin(base, "zxgklanmu/index.html"),
            urljoin(base, "zdfg/index.html"),
        ]

    def parse_api_response(
        self,
        body: str,
    ) -> tuple[bool, list[dict], str]:
        try:
            response = json.loads(body)
        except (json.JSONDecodeError, TypeError, UnicodeError):
            return False, [], "FreeCMS 搜索接口返回了无效 JSON"
        if not isinstance(response, dict):
            return False, [], "FreeCMS 搜索接口业务失败"
        if str(response.get("code")) not in {"0", "200"}:
            return (
                False,
                [],
                str(response.get("msg") or "FreeCMS 搜索接口业务失败"),
            )
        payload = response.get("data")
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        return True, list(rows) if isinstance(rows, list) else [], ""


class YunnanCmsAdapter(SiteAdapter):
    profile = "yunnan-cms"

    def __init__(self, start_url: str):
        self.origin = _origin(start_url)

    def search_specs(self) -> list[SearchSpec]:
        return [
            SearchSpec(
                "site-search",
                self.origin + "/searchN.aspx",
                "tags",
                "page",
                (("type", ""),),
            )
        ]


def select_adapter(start_url: str, homepage: str) -> SiteAdapter:
    parts = _safe_urlsplit(start_url)
    host = (parts.hostname or "").lower().rstrip(".") if parts else ""
    lowered = homepage.lower()
    if (
        host == "zycg.gov.cn"
        or host.endswith(".zycg.gov.cn")
        or "searchall.do" in lowered
    ):
        return FreeCmsAdapter(start_url)
    if "searchn.aspx" in lowered or "searchclasscount.aspx" in lowered:
        return YunnanCmsAdapter(start_url)
    return SiteAdapter()
