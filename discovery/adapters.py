from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import SplitResult, quote, urljoin, urlsplit

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

    def domain_policy(self, start_url: str) -> DomainPolicy:
        policy = super().domain_policy(start_url)
        # searchAll.do 常返回 mkt.zycg.gov.cn 等同源内容子域公告页
        return DomainPolicy(
            policy.root_host,
            policy.allowed_hosts,
            policy.excluded_hosts,
            allow_related_hosts=True,
        )

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

    def recent_notice_spec(self) -> SearchSpec | None:
        host = urlsplit(self.origin).hostname or ""
        if host != "zycg.gov.cn" and not host.endswith(".zycg.gov.cn"):
            return None
        return SearchSpec(
            "freecms-recent",
            self.origin
            + "/freecms/rest/v1/notice/selectInfoMore.do",
            "title",
            "currPage",
            (
                ("siteId", "6f5243ee-d4d9-4b69-abbd-1e40576ccd7d"),
                ("channel", "d0e7c5f4-b93e-4478-b7fe-61110bb47fd5"),
                ("pageSize", "15"),
                ("implementWay", "1"),
                ("noticeType", "1,2,3,31,32,52,57,61"),
            ),
        )

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
            return False, [], "FreeCMS 搜索接口业务失败"
        payload = response.get("data")
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
        ):
            return False, [], "FreeCMS 搜索接口数据结构无效"
        return True, list(rows), ""

    def parse_recent_response(
        self,
        body: str,
    ) -> tuple[bool, list[dict], str]:
        try:
            response = json.loads(body)
        except (json.JSONDecodeError, TypeError, UnicodeError):
            return False, [], "FreeCMS 最近公告接口返回了无效 JSON"
        if not isinstance(response, dict):
            return False, [], "FreeCMS 最近公告接口业务失败"
        if str(response.get("code")) not in {"0", "200"}:
            return False, [], "FreeCMS 最近公告接口业务失败"
        rows = response.get("data")
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
        ):
            return False, [], "FreeCMS 最近公告接口数据结构无效"

        normalized_rows = []
        for row in rows:
            normalized = dict(row)
            page_url = normalized.get("pageUrl")
            if page_url is None:
                page_url = normalized.get("pageURL")
            if page_url is None:
                page_url = normalized.get("pageurl")
            normalized.pop("pageURL", None)
            normalized.pop("pageurl", None)
            if page_url is not None:
                normalized["pageUrl"] = page_url
            normalized_rows.append(normalized)
        return True, normalized_rows, ""


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


class YngpAdapter(SiteAdapter):
    """云南省政府采购网（yngp.com）：公告列表由 bootgrid AJAX 接口驱动。

    列表页 /page/procurement/procurementList.html 静态 HTML 里没有公告
    链接，数据由 /api/procurement/Procurement.gghtMoreList.svc 以 JSON
    返回；详情页地址按列表 JS（procurementList.js 的 show()）中的
    tabletype 规则拼接。
    """

    profile = "yngp"

    def __init__(self, start_url: str):
        self.origin = _origin(start_url)

    @property
    def list_url(self) -> str:
        return self.origin + "/page/procurement/procurementList.html"

    @property
    def warm_url(self) -> str:
        return self.origin + "/api/common/otheruse.getdistrictlist.svc"

    @property
    def api_url(self) -> str:
        return self.origin + "/api/procurement/Procurement.gghtMoreList.svc"

    def detail_url(self, row: dict) -> str:
        bulletin_id = str(row.get("bulletin_id") or "").strip()
        if not bulletin_id:
            return ""
        tabletype = str(row.get("tabletype") or "").strip()
        bulletinclass = str(row.get("bulletinclass") or "").strip()
        bid = quote(bulletin_id)
        if tabletype in {"3", "4"} and bulletinclass == "bxlx014":
            path = f"/showContractDetailInfo.html?bulletin_id={bid}"
        elif tabletype == "8":
            path = f"/showAcceptanceResultsNoticeInfo.html?bulletinid={bid}"
        elif tabletype in {"9", "12", "13", "14", "15", "19"}:
            path = f"/showZCYBulletinInfo.html?bulletin_id={bid}"
        elif tabletype in {"11", "16", "17", "18", "22", "24"}:
            path = (
                f"/showZCYManageBulletinInfo.html?bulletin_id={bid}"
                f"&tabletype={tabletype}"
            )
        elif tabletype == "21":
            path = f"/showContractChangeNoticeInfo.html?bulletin_id={bid}"
        elif tabletype == "23":
            path = f"/viewPurchaseInfo.html?sys_purchaseintention_id={bid}"
        else:
            # 站内跳转 /ggmxinfo.html?bulletinid=，301 到该详情地址
            path = f"/showBulletinInfo.html?bulletin_id={bid}"
        return self.origin + path

    def parse_api_response(
        self,
        body: str,
    ) -> tuple[bool, int | None, list[dict], str]:
        try:
            response = json.loads(body)
        except (json.JSONDecodeError, TypeError, UnicodeError):
            return False, None, [], "YNGP 采购接口返回了无效 JSON"
        if not isinstance(response, dict):
            return False, None, [], "YNGP 采购接口业务失败"
        if str(response.get("code")) != "1":
            return False, None, [], "YNGP 采购接口业务失败"
        payload = response.get("data")
        if not isinstance(payload, dict):
            return False, None, [], "YNGP 采购接口数据结构无效"
        rows = payload.get("rows")
        if rows is None:
            # 会话未通过服务端校验时返回 {"code":"1","data":{}}；
            # 正常空结果的 data 中带有 "rows": []
            return False, None, [], "YNGP 采购接口会话校验未通过"
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            return False, None, [], "YNGP 采购接口数据结构无效"
        raw_total = payload.get("total")
        total: int | None = None
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError):
                total = None
        return True, total, list(rows), ""


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
    if (
        "searchn.aspx" in lowered
        or "searchclasscount.aspx" in lowered
        # 云南 CMS 模板族的站内搜索入口：HSearchGo/MSearchGo 跳转
        # Search.html?tags=（搜索结果由 searchN.aspx 提供）
        or "hsearchgo" in lowered
        or "msearchgo" in lowered
    ):
        return YunnanCmsAdapter(start_url)
    if (
        host == "yngp.com"
        or host.endswith(".yngp.com")
        # 列表页由 bootgrid AJAX 接口驱动，首页/列表页脚本引用该接口
        or "procurement.gghtmorelist.svc" in lowered
    ):
        return YngpAdapter(start_url)
    return SiteAdapter()
