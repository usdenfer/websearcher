from __future__ import annotations

import re
from urllib.parse import (
    parse_qsl,
    urlencode,
    urldefrag,
    urljoin,
    urlsplit,
    urlunsplit,
)

from bs4 import BeautifulSoup

from discovery.models import DomainPolicy

TRACKING_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "spm",
    "from",
    "source",
}
MULTIPART_SUFFIXES = {
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "co.uk",
    "org.uk",
    "com.au",
    "co.jp",
}
BINARY_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
}
AUTH_PATH_SEGMENTS = {"login", "signin", "register", "passport", "user"}
EXCLUDED_HOST_LABELS = {
    "ad",
    "ads",
    "advert",
    "login",
    "passport",
    "account",
    "shop",
    "mall",
    "pay",
    "cps",
    "tracking",
}


def normalize_candidate_url(url: str) -> str:
    clean, _fragment = urldefrag(url.strip())
    parts = urlsplit(clean)
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS
    ]
    query.sort()
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def is_html_candidate(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return False
    path = parts.path.lower()
    if any(path.endswith(suffix) for suffix in BINARY_SUFFIXES):
        return False
    segments = {segment for segment in path.split("/") if segment}
    return not segments.intersection(AUTH_PATH_SEGMENTS)


def registrable_domain(host: str) -> str:
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    suffix = ".".join(labels[-2:])
    take = 3 if suffix in MULTIPART_SUFFIXES else 2
    return ".".join(labels[-take:])


def url_allowed(url: str, policy: DomainPolicy) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if not is_html_candidate(url) or not host:
        return False
    if policy.allows(host):
        return True
    excluded = {item.lower().rstrip(".") for item in policy.excluded_hosts}
    if host in excluded:
        return False
    labels = set(re.split(r"[-.]", host))
    return (
        policy.allow_related_hosts
        and not labels.intersection(EXCLUDED_HOST_LABELS)
        and registrable_domain(host) == registrable_domain(policy.root_host)
    )


def extend_policy_with_declared_urls(
    policy: DomainPolicy,
    urls: list[str],
) -> DomainPolicy:
    root_domain = registrable_domain(policy.root_host)
    allowed = set(policy.allowed_hosts)
    for url in urls:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        if host and registrable_domain(host) == root_domain:
            allowed.add(host)
    return DomainPolicy(
        policy.root_host,
        frozenset(allowed),
        policy.excluded_hosts,
        allow_related_hosts=True,
    )


def canonical_url(
    html: str,
    page_url: str,
    policy: DomainPolicy,
) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("link", rel=lambda value: value and "canonical" in value)
    original = normalize_candidate_url(page_url)
    if node is None or not node.get("href"):
        return original
    candidate = normalize_candidate_url(urljoin(page_url, node["href"]))
    return candidate if url_allowed(candidate, policy) else original
