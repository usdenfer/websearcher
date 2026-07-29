"""Shared end-to-end search budget selection."""
from __future__ import annotations

from urllib.parse import urlsplit

from crawler import ARCHIVE_BUDGET_SECONDS, ARCHIVE_MAX_PAGES
from discovery.models import BudgetManager

SEARCH_BUDGET_SECONDS = 120
BASE_BFS_PAGE_BUDGET = 30


def _normalize_dns_hostname(hostname: str) -> str | None:
    if hostname.endswith("."):
        hostname = hostname[:-1]
    try:
        normalized = hostname.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if len(normalized) > 253:
        return None
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(
            character.isascii()
            and (character.isalnum() or character == "-")
            for character in label
        )
        for label in labels
    ):
        return None
    return normalized


def make_search_budget(
    start_url: str,
    archive_mode: bool,
    started_at: float,
) -> BudgetManager:
    if archive_mode:
        return BudgetManager(
            initial_pages=ARCHIVE_MAX_PAGES,
            max_pages=ARCHIVE_MAX_PAGES,
            timeout_seconds=ARCHIVE_BUDGET_SECONDS,
            started_at=started_at,
        )
    try:
        parts = urlsplit(start_url)
        hostname = parts.hostname
        _ = parts.port
    except (TypeError, UnicodeError, ValueError):
        hostname = None
        parts = None
    if (
        parts is None
        or parts.scheme.lower() not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or hostname is None
    ):
        hostname = None
    else:
        hostname = _normalize_dns_hostname(hostname)
    if hostname == "zycg.gov.cn" or (
        hostname is not None
        and hostname.endswith(".zycg.gov.cn")
    ):
        return BudgetManager(
            initial_pages=150,
            max_pages=300,
            timeout_seconds=300,
            started_at=started_at,
        )
    return BudgetManager(
        initial_pages=60,
        max_pages=120,
        timeout_seconds=SEARCH_BUDGET_SECONDS,
        started_at=started_at,
    )
