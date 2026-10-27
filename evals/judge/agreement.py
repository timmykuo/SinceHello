"""Agreement between the judge and a reference: human ratings or hand labels (SPEC.md §10.3).

Both sides are rows of `{scores: {dimension: 1-5}, keep: bool}` keyed by insight or case
id; either side may leave a dimension or `keep` out (None), and only pairs where both
have a value count. Per dimension: exact agreement, agreement within one point, and the
mean absolute difference (judge minus reference, so a positive `bias` means the judge
scores higher). `keep` agreement is the share of matching 👍/👎.
"""

from __future__ import annotations

from statistics import mean
from typing import Any

from evals.judge.judge import DIMENSIONS


def agreement(
    judge: dict[str, dict[str, Any]], reference: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    ids = [i for i in reference if i in judge and judge[i].get("scores")]
    dims: dict[str, Any] = {}
    for d in DIMENSIONS:
        pairs = [
            (judge[i]["scores"][d], reference[i]["scores"][d])
            for i in ids
            if (reference[i].get("scores") or {}).get(d) is not None
        ]
        if not pairs:
            continue
        diffs = [j - r for j, r in pairs]
        dims[d] = {
            "n": len(pairs),
            "exact": round(sum(x == 0 for x in diffs) / len(diffs), 3),
            "within_1": round(sum(abs(x) <= 1 for x in diffs) / len(diffs), 3),
            "mae": round(mean(abs(x) for x in diffs), 2),
            "bias": round(mean(diffs), 2),
        }
    keeps = [
        (judge[i]["keep"], reference[i]["keep"])
        for i in ids
        if reference[i].get("keep") is not None and judge[i].get("keep") is not None
    ]
    return {
        "n": len(ids),
        "dimensions": dims,
        "within_1": (
            round(mean(v["within_1"] for v in dims.values()), 3) if dims else None
        ),  # mean over dimensions
        "keep": {
            "n": len(keeps),
            "agreement": round(sum(j == r for j, r in keeps) / len(keeps), 3) if keeps else None,
        },
    }
