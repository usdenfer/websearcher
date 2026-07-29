from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import SplitResult, urljoin, urlsplit

from discovery.models import DomainPolicy, SearchSpec

HOST_LABEL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def _safe_urlsplit(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
        _hostname = parts.hostname
        _port = parts.port
    except (UnicodeError, ValueError):
        return None
    return parts


def _safe_http_authority(
    start_url: str,
) -> tuple[str, str, int | None] | None:
    parts = _safe_urlsplit(start_url)
    if (
        parts is None
        or parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    host = parts.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None
        labels = host.split(".")
        if not labels or any(
            not HOST_LABEL_RE.fullmatch(label) for label in labels
        ):
            return None
    return parts.scheme.lower(), host, parts.port


def _origin(start_url: str) -> str:
    authority = _safe_http_authority(start_url)
    if authority is None:
        raise ValueError("适配器起始地址必须是有效的 HTTP(S) URL")
    scheme, host, port = authority
    formatted_host = f"[{host}]" if ":" in host else host
    port_suffix = f":{port}" if port is not None else ""
    return f"{scheme}://{formatted_host}{port_suffix}"


class SiteAdapter:
    profile = "generic"

    def domain_policy(self, start_url: str) -> DomainPolicy:
        authority = _safe_http_authority(start_url)
        host = authority[1] if authority else ""
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
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
        ):
            return False, [], "FreeCMS 搜索接口数据结构无效"
        return True, list(rows), ""


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
