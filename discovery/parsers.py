from __future__ import annotations

import re
from urllib.parse import urljoin
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from discovery.models import Candidate, DomainPolicy, SearchSpec
from discovery.urltools import normalize_candidate_url, url_allowed

SEARCH_NAMES = {
    "q",
    "s",
    "key",
    "keyword",
    "keywords",
    "query",
    "searchcontent",
}
SEARCH_WORDS = ("搜索", "检索", "search")
CATEGORY_WORDS = (
    "新闻",
    "资讯",
    "公告",
    "采购",
    "攻略",
    "法规",
    "信息公开",
    "news",
    "article",
    "notice",
    "guide",
)
SCRIPT_SEARCH_RE = re.compile(
    r"""(?P<quote>['"])(?P<url>[^'"]*(?:search|find|query)[^'"]*?)"""
    r"""\?(?P<param>q|s|key|keyword|keywords|query|searchContent)=""",
    re.IGNORECASE,
)


def _safe_absolute_url(base_url: str, target: object) -> str:
    try:
        joined = urljoin(base_url, str(target))
    except (UnicodeError, ValueError):
        return ""
    return normalize_candidate_url(joined)


def _has_search_semantics(node: object) -> bool:
    text = getattr(node, "get_text")(" ", strip=True).lower()
    markup = str(node).lower()
    return any(word in text or word in markup for word in SEARCH_WORDS)


def detect_search_specs(html: str, base_url: str) -> list[SearchSpec]:
    soup = BeautifulSoup(html, "html.parser")
    specs: list[SearchSpec] = []
    for form in soup.find_all("form"):
        if str(form.get("method") or "get").lower() != "get":
            continue
        query_input = next(
            (
                item
                for item in form.find_all(attrs={"name": True})
                if str(item["name"]).lower() in SEARCH_NAMES
            ),
            None,
        )
        if query_input is None or not _has_search_semantics(form):
            continue
        action = _safe_absolute_url(
            base_url,
            form.get("action") or base_url,
        )
        if action:
            specs.append(
                SearchSpec(
                    "site-search",
                    action,
                    str(query_input["name"]),
                )
            )

    for node in soup.select("[data-action]"):
        query_input = next(
            (
                item
                for item in node.find_all(attrs={"name": True})
                if str(item["name"]).lower() in SEARCH_NAMES
            ),
            None,
        )
        if query_input is None:
            continue
        action = _safe_absolute_url(base_url, node["data-action"])
        if action:
            specs.append(
                SearchSpec(
                    "site-search",
                    action,
                    str(query_input["name"]),
                )
            )

    for script in soup.find_all("script"):
        for match in SCRIPT_SEARCH_RE.finditer(script.get_text()):
            action = _safe_absolute_url(base_url, match.group("url"))
            if action:
                specs.append(
                    SearchSpec(
                        "site-search",
                        action,
                        match.group("param"),
                    )
                )

    result: list[SearchSpec] = []
    seen: set[tuple[str, str, str | None]] = set()
    for spec in specs:
        identity = (
            spec.url,
            spec.query_param.lower(),
            spec.page_param,
        )
        if identity not in seen:
            seen.add(identity)
            result.append(spec)
    return result


def detect_feed_urls(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[str] = []
    for link in soup.find_all("link", href=True):
        rel = link.get("rel", [])
        rel_tokens = rel.split() if isinstance(rel, str) else rel
        media_type = str(link.get("type") or "").lower()
        if (
            "alternate" not in {str(token).lower() for token in rel_tokens}
            or ("rss" not in media_type and "atom" not in media_type)
        ):
            continue
        url = _safe_absolute_url(base_url, link["href"])
        if url and url not in result:
            result.append(url)
    return result


def detect_category_urls(
    html: str,
    base_url: str,
    policy: DomainPolicy,
) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[str] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True).lower()
        href = str(anchor["href"]).lower()
        if not any(word in text or word in href for word in CATEGORY_WORDS):
            continue
        url = _safe_absolute_url(base_url, anchor["href"])
        if url and url_allowed(url, policy) and url not in result:
            result.append(url)
    return result[:12]


def parse_result_candidates(
    html: str,
    base_url: str,
    policy: DomainPolicy,
    source: str,
    keyword: str,
) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(
        "article, main li, .result, .search-result, .titlist li"
    )
    anchors = [
        anchor
        for container in containers
        for anchor in container.find_all("a", href=True)
    ]
    if not anchors:
        anchors = soup.find_all("a", href=True)

    result: list[Candidate] = []
    seen: set[str] = set()
    for anchor in anchors:
        if anchor is None:
            continue
        url = _safe_absolute_url(base_url, anchor["href"])
        if not url or url in seen or not url_allowed(url, policy):
            continue
        seen.add(url)
        title = anchor.get_text(" ", strip=True)
        score = 100 if keyword.lower() in title.lower() else 70
        result.append(
            Candidate(
                url,
                source,
                keyword,
                title,
                score,
            )
        )
    return result


def parse_pagination(
    html: str,
    base_url: str,
    policy: DomainPolicy,
) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[str] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        classes = " ".join(str(item) for item in anchor.get("class", []))
        if not (
            text in {"下一页", "下页", ">", "»"}
            or text.isdigit()
            or "page" in classes.lower()
        ):
            continue
        url = _safe_absolute_url(base_url, anchor["href"])
        if url and url_allowed(url, policy) and url not in result:
            result.append(url)
    return result


def parse_sitemap(xml: str, policy: DomainPolicy) -> list[str]:
    root = _parse_xml(xml)
    if root is None:
        return []
    result: list[str] = []
    for loc in root.iter():
        if _local_name(loc.tag) != "loc":
            continue
        url = normalize_candidate_url((loc.text or "").strip())
        if url and url_allowed(url, policy) and url not in result:
            result.append(url)
    return result


def parse_sitemap_index(xml: str, policy: DomainPolicy) -> list[str]:
    root = _parse_xml(xml)
    if root is None or _local_name(root.tag) != "sitemapindex":
        return []
    result: list[str] = []
    for sitemap in root:
        if _local_name(sitemap.tag) != "sitemap":
            continue
        loc = next(
            (
                child
                for child in sitemap
                if _local_name(child.tag) == "loc"
            ),
            None,
        )
        if loc is None:
            continue
        url = normalize_candidate_url((loc.text or "").strip())
        if url and url_allowed(url, policy) and url not in result:
            result.append(url)
    return result


def parse_feed(xml: str, policy: DomainPolicy) -> list[Candidate]:
    root = _parse_xml(xml)
    if root is None:
        return []
    result: list[Candidate] = []
    seen: set[str] = set()
    for item in root.iter():
        item_type = _local_name(item.tag)
        if item_type not in {"item", "entry"}:
            continue
        raw_url = ""
        links = [
            child
            for child in item
            if _local_name(child.tag) == "link"
        ]
        if item_type == "entry":
            link = next(
                (
                    node
                    for node in links
                    if node.get("href")
                    and str(node.get("rel") or "alternate").lower()
                    == "alternate"
                ),
                None,
            )
            if link is not None:
                raw_url = str(link.get("href"))
        elif links:
            raw_url = str(links[0].text or "").strip()
        url = normalize_candidate_url(raw_url)
        if (
            not url
            or url in seen
            or not url_allowed(url, policy)
        ):
            continue
        seen.add(url)
        title_node = next(
            (
                child
                for child in item
                if _local_name(child.tag) == "title"
            ),
            None,
        )
        title = (
            " ".join("".join(title_node.itertext()).split())
            if title_node is not None
            else ""
        )
        result.append(
            Candidate(
                url,
                "feed",
                title_hint=title,
                score=65,
            )
        )
    return result


def _parse_xml(xml: str) -> ElementTree.Element | None:
    try:
        return ElementTree.fromstring(xml)
    except (ElementTree.ParseError, UnicodeError, ValueError):
        return None


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()
