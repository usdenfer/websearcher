from __future__ import annotations

import time
from dataclasses import dataclass, field


_STOP_REASON_PRIORITY = {
    None: 0,
    "date-boundary": 10,
    "provider-page-limit": 20,
    "channel-failure": 30,
    "html-page-budget": 40,
    "time-budget": 50,
}


@dataclass(frozen=True)
class Candidate:
    url: str
    source: str = field(compare=False)
    keyword: str = field(default="", compare=False)
    title_hint: str = field(default="", compare=False)
    score: int = field(default=0, compare=False)
    requires_render: bool = field(default=False, compare=False)
    section: str = field(default="", compare=False)
    published_date: str | None = field(default=None, compare=False)
    source_evidence: tuple[str, ...] = field(
        default=(), compare=False
    )
    district: str = field(default="", compare=False)


@dataclass(frozen=True)
class SearchSpec:
    source: str
    url: str
    query_param: str
    page_param: str | None = None
    fixed_params: tuple[tuple[str, str], ...] = ()
    result_selector: str = "a[href]"

    def params_for(self, keyword: str, page: int = 1) -> dict[str, str | int]:
        params: dict[str, str | int] = dict(self.fixed_params)
        params[self.query_param] = keyword
        if self.page_param is not None:
            params[self.page_param] = page
        return params


@dataclass(frozen=True)
class DomainPolicy:
    root_host: str
    allowed_hosts: frozenset[str]
    excluded_hosts: frozenset[str] = frozenset()
    allow_related_hosts: bool = False

    def allows(self, host: str) -> bool:
        normalized = host.lower().rstrip(".")
        allowed = {item.lower().rstrip(".") for item in self.allowed_hosts}
        excluded = {item.lower().rstrip(".") for item in self.excluded_hosts}
        return normalized in allowed and normalized not in excluded


@dataclass
class DiscoveryStats:
    profile: str = "generic"
    sources_tried: set[str] = field(default_factory=set)
    sources_succeeded: set[str] = field(default_factory=set)
    candidates_found: int = 0
    candidates_fetched: int = 0
    rendered_pages: int = 0
    budget_expanded: bool = False
    partial: bool = False
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    recent_window_days: int | None = None
    weak_candidates: int = 0
    unknown_date_candidates: int = 0
    stop_reason: str | None = None

    def note_stop(self, reason: str) -> None:
        current_priority = _STOP_REASON_PRIORITY.get(
            self.stop_reason, 0
        )
        reason_priority = _STOP_REASON_PRIORITY.get(reason, 0)
        if reason_priority > current_priority:
            self.stop_reason = reason

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "sourcesTried": sorted(self.sources_tried),
            "sourcesSucceeded": sorted(self.sources_succeeded),
            "candidatesFound": self.candidates_found,
            "candidatesFetched": self.candidates_fetched,
            "renderedPages": self.rendered_pages,
            "budgetExpanded": self.budget_expanded,
            "partial": self.partial,
            "elapsedMs": self.elapsed_ms,
            "warnings": list(self.warnings),
            "recentWindowDays": self.recent_window_days,
            "weakCandidates": self.weak_candidates,
            "unknownDateCandidates": self.unknown_date_candidates,
            "stopReason": self.stop_reason,
        }


@dataclass
class BudgetManager:
    initial_pages: int = 3000
    max_pages: int = 5000
    timeout_seconds: float = 86400.0
    used_html_pages: int = 0
    page_limit: int = field(init=False)
    started_at: float = field(default_factory=time.monotonic)
    provider_requests: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.page_limit = self.initial_pages

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    @property
    def deadline(self) -> float:
        return self.started_at + self.timeout_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def reserve_html(self) -> bool:
        if self.expired() or self.used_html_pages >= self.page_limit:
            return False
        self.used_html_pages += 1
        return True

    def reserve_provider(self, source: str, limit: int) -> bool:
        used = self.provider_requests.get(source, 0)
        if self.expired() or used >= limit:
            return False
        self.provider_requests[source] = used + 1
        return True

    def expand(self, high_value_remaining: int) -> bool:
        if (
            self.expired()
            or high_value_remaining <= 0
            or self.page_limit >= self.max_pages
        ):
            return False
        self.page_limit = self.max_pages
        return True
