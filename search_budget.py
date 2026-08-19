"""Shared end-to-end search budget selection."""
from __future__ import annotations

from crawler import ARCHIVE_BUDGET_SECONDS, ARCHIVE_MAX_PAGES
from discovery.models import BudgetManager

SEARCH_BUDGET_SECONDS = 86400
BASE_BFS_PAGE_BUDGET = 5000


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
    return BudgetManager(
        initial_pages=3000,
        max_pages=5000,
        timeout_seconds=SEARCH_BUDGET_SECONDS,
        started_at=started_at,
    )
