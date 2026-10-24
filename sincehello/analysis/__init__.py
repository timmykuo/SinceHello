"""Local analysis (SPEC.md §7.1–7.3)."""

from sincehello.analysis.stats import (
    STATS,
    StatResult,
    compute,
    compute_all,
    query_id,
    recompute,
    resolve_params,
)

__all__ = [
    "STATS",
    "StatResult",
    "compute",
    "compute_all",
    "query_id",
    "recompute",
    "resolve_params",
]
