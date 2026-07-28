from __future__ import annotations

import ipaddress
import re
from urllib.parse import (
    SplitResult,
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


def _safe_urlsplit(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
        # Accessing these properties performs additional authority validation.
        _hostname = parts.hostname
        _port = parts.port
    except (UnicodeError, ValueError):
        return None
    return parts


def _safe_urljoin(base: str, target: str) -> str | None:
    try:
        return urljoin(base, target)
    except (UnicodeError, ValueError):
        return None


def canonical_host(host: str) -> str | None:
    normalized = host.lower().rstrip(".")
    if not normalized:
        return None
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        try:
            return normalized.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return None


def canonical_authority(url: str) -> tuple[str, int] | None:
    """Return a credential-free, IDNA/IP-normalized host and effective port."""
    parts = _safe_urlsplit(url)
    if (
        parts is None
        or parts.scheme.lower() not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    host = canonical_host(parts.hostname or "")
    if host is None:
        return None
    default_port = 443 if parts.scheme.lower() == "https" else 80
    return host, parts.port if parts.port is not None else default_port


def same_site_boundary(first_url: str, second_url: str) -> bool:
    """Allow redirects within one registrable domain, or the exact same IP."""
    first = canonical_authority(first_url)
    second = canonical_authority(second_url)
    if first is None or second is None:
        return False
    first_host, _first_port = first
    second_host, _second_port = second
    try:
        first_ip = ipaddress.ip_address(first_host)
    except ValueError:
        first_ip = None
    try:
        second_ip = ipaddress.ip_address(second_host)
    except ValueError:
        second_ip = None
    if first_ip is not None or second_ip is not None:
        return first_ip is not None and first_ip == second_ip
    return registrable_domain(first_host) == registrable_domain(second_host)


def _normalized_netloc(parts: SplitResult) -> str:
    host = parts.hostname
    if host is None:
        return parts.netloc
    userinfo, separator, _authority = parts.netloc.rpartition("@")
    prefix = f"{userinfo}@" if separator else ""
    normalized_host = host.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{prefix}{normalized_host}{port}"


def normalize_candidate_url(url: str) -> str:
    try:
        clean, _fragment = urldefrag(url.strip())
    except (UnicodeError, ValueError):
        return ""
    parts = _safe_urlsplit(clean)
    if parts is None:
        return ""
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS
    ]
    query.sort(key=lambda item: item[0])
    return urlunsplit(
        (
            parts.scheme.lower(),
            _normalized_netloc(parts),
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def is_html_candidate(url: str) -> bool:
    parts = _safe_urlsplit(url)
    if parts is None:
        return False
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
    parts = _safe_urlsplit(url)
    if parts is None:
        return False
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
        parts = _safe_urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".") if parts else ""
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
    node = None
    for link in soup.find_all("link"):
        rel = link.get("rel", [])
        tokens = rel.split() if isinstance(rel, str) else rel
        if any(str(token).lower() == "canonical" for token in tokens):
            node = link
            break
    original = normalize_candidate_url(page_url)
    if node is None or not node.get("href"):
        return original
    joined = _safe_urljoin(page_url, str(node["href"]))
    if joined is None:
        return original
    candidate = normalize_candidate_url(joined)
    return candidate if url_allowed(candidate, policy) else original
