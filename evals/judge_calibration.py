"""Calibration set for the eval judge (SPEC.md §10.2-10.3) on the bean-goose sample.

The critic's hand-written cases (`critic_calibration.CASES`, all invented, all passing
the deterministic checks) plus two the judge must score on dimensions the critic has no
verdict for: an accurate but obvious card (non-obviousness) and a mean one (tone). Each
case carries the scores and `keep` a careful human would give; a dimension left out is
too debatable to label. Labels are deliberately coarse: agreement counts within one point.

`run()` scores each case once under a hard dollar cap and reports agreement with the
labels (`agreement.agreement`). Run it with `sincehello eval judge-calibration`. The
same agreement against your own ratings of real runs is `sincehello eval agreement`.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import critic_calibration as critic_cal
from evals.judge.agreement import agreement
from evals.judge.judge import LLMJudge, judge_insights
from sincehello.agent.context import ToolContext
from sincehello.report.schema import Insight


@dataclass(frozen=True)
class Label:
    keep: bool | None
    scores: dict[str, int]
    why: str


L = Label
LABELS: dict[str, Label] = {
    "ily_correct": L(
        True,
        {"accuracy": 5, "specificity": 5, "delight": 5, "tone": 5, "non_obviousness": 3},
        "True, dated, with the 41-second reply; they know it happened, not the details.",
    ),
    "goose_correct": L(
        True,
        {"accuracy": 5, "specificity": 5, "delight": 5, "non_obviousness": 4},
        "Their own joke, with its origin and a count they never kept.",
    ),
    "fight_honest": L(
        True,
        {"accuracy": 5, "specificity": 5, "tone": 4, "non_obviousness": 3},
        "A hard moment told plainly is a good card.",
    ),
    "wrong_sender": L(False, {"accuracy": 2}, "Alex said it first, not you."),
    "wrong_date": L(False, {"accuracy": 2}, "May 14, not June 3."),
    "sarcasm": L(False, {"accuracy": 1}, '"very glamorous" is self-deprecating.'),
    "playlist_decoy": L(False, {"accuracy": 1}, '"love your playlist" is not "I love you".'),
    "group_chat_trap": L(False, {"accuracy": 1}, "Nothing cited supports the claim."),
    "goose_misattributed": L(False, {"accuracy": 2}, "Alex coined it; you echoed it."),
    "number_without_metric": L(None, {"accuracy": 3}, "22 has nothing measuring it."),
    "bland_plant": L(
        False,
        {"accuracy": 5, "specificity": 2, "delight": 1, "non_obviousness": 1},
        "True and dull.",
    ),
    "obvious_volume": L(
        False,
        {"accuracy": 5, "specificity": 1, "delight": 1, "non_obviousness": 1},
        "Of course they text; it says nothing about these two.",
    ),
    "mean_sushi": L(
        False,
        {"accuracy": 3, "tone": 1, "delight": 1},
        "Mocks Alex over a self-deprecating line.",
    ),
}

EXTRA_CASES: list[critic_cal.Case] = [
    critic_cal.Case(
        "obvious_volume",
        "pass",
        "Accurate and measured, and tells them nothing.",
        {
            "category": "rhythm",
            "kind": "count",
            "title": "You text",
            "headline": "You two send each other messages",
            "body": "Lots of them, as the count shows.",
            "evidence": [critic_cal._ev("000502", "my plant is dying!!")],
        },
        count=".",
    ),
    critic_cal.Case(
        "mean_sushi",
        "reject",
        "Mocking.",
        {
            "category": "free",
            "kind": "moment",
            "title": "Dinner, Alex-style",
            "headline": "Alex can't even eat dinner like an adult",
            "body": 'Sushi over the sink. "Very glamorous", Alex. Sure.',
            "evidence": [
                critic_cal._ev("000167", "ate sushi standing over the sink, very glamorous")
            ],
        },
    ),
]
CASES: list[critic_cal.Case] = [*critic_cal.CASES, *EXTRA_CASES]


def prepare(ctx: ToolContext) -> list[Insight]:
    """Every case as an Insight id'd `cal_<case>` (raises if one fails the checks)."""
    return [critic_cal.prepare(ctx, c) for c in CASES]


def reference() -> dict[str, dict[str, Any]]:
    return {f"cal_{k}": {"scores": v.scores, "keep": v.keep} for k, v in LABELS.items()}


def run(judge: LLMJudge) -> dict[str, Any]:
    """Score every case with `judge` (whose budget is the cap) and compare with the labels."""
    with tempfile.TemporaryDirectory() as tmp:
        store, pid = critic_cal.open_sample(Path(tmp))
        try:
            ctx = ToolContext(store, pid, "America/New_York")
            insights = prepare(ctx)
            out = judge_insights(judge, insights, ctx)
        finally:
            store.close()
    by_id = {r["insight"]: r for r in out["insights"]}
    out["agreement"] = agreement(by_id, reference())
    return out
