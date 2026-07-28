from .models import (
    BudgetManager,
    Candidate,
    DiscoveryStats,
    DomainPolicy,
    SearchSpec,
)

__all__ = [
    "BudgetManager",
    "Candidate",
    "DiscoveryRun",
    "DiscoveryStats",
    "DomainPolicy",
    "SearchSpec",
    "discover_pages",
]


def __getattr__(name: str):
    if name in {"DiscoveryRun", "discover_pages"}:
        from discovery.engine import DiscoveryRun, discover_pages

        return {
            "DiscoveryRun": DiscoveryRun,
            "discover_pages": discover_pages,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
