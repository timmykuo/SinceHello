"""Calibration set for the LLM critic (SPEC.md §7.6, §10.2) on the bean-goose sample.

Hand-written insights with a label each: some correct, some subtly wrong (wrong sender,
wrong date, sarcasm, a near-miss "love your", the group-chat "love you" trap, a
misattributed inside joke, a number without a metric), and some accurate but bland.
Every case passes the deterministic checks (`tests/test_critic_calibration.py`), so
each one is something only the critic can catch.

`run()` sends each case to the critic once (no revise loop) under a hard dollar cap and
reports agreement with the labels. Run it with `sincehello eval critic`.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sincehello.agent.budget import Budget
from sincehello.agent.context import ToolContext
from sincehello.agent.tools import CriticUnavailable, Verdict, count
from sincehello.report.schema import Insight, InsightDraft
from sincehello.store import Store
from sincehello.verify import checks

SID = "synthetic:bean-goose-s42-"
ILY = r"\b(i love you|love you|ily|love u)\b"


@dataclass(frozen=True)
class Case:
    id: str
    label: str  # the verdict we expect: pass | revise | reject
    why: str
    insight: dict[str, Any]
    count: str | None = None  # a count() pattern whose query_id goes in the metric
    accept: tuple[str, ...] = field(default=())  # other verdicts we'd also accept

    @property
    def ok(self) -> tuple[str, ...]:
        return (self.label, *self.accept)


def _ev(n: str, quote: str) -> dict[str, str]:
    return {"message_id": SID + n, "quote": quote}


def _ily(headline: str, body: str = 'You answered 41 seconds later: "i love you too 🥹".'):
    return {
        "category": "milestone",
        "kind": "first_occurrence",
        "title": 'The first "I love you"',
        "headline": headline,
        "body": body,
        "evidence": [_ev("000507", "i love you"), _ev("000509", "i love you too 🥹")],
        "first": {"pattern": ILY, "sender": "any"},
    }


CASES: list[Case] = [
    Case(
        "ily_correct",
        "pass",
        "Matches the planted fact: Alex, May 14 11:48pm Alex's time, reply 41s later.",
        _ily("Alex said it first: May 14, 2021, at 11:48pm"),
    ),
    Case(
        "goose_correct",
        "pass",
        "Alex set it up and named it; the count comes from the metric.",
        {
            "category": "language",
            "kind": "inside_joke",
            "title": "The goose incident",
            "headline": 'Alex named "the goose incident" on April 3, 2021',
            "body": "A goose chased Alex across a parking lot and made them drop their"
            " coffee. It has come up 32 times since.",
            "evidence": [
                _ev("000224", "a goose just chased me across the entire parking lot"),
                _ev("000226", "it hissed. i dropped my whole coffee"),
                _ev("000227", "we are calling this the goose incident"),
            ],
        },
        count=r"\bgoose incident\b",
    ),
    Case(
        "fight_honest",
        "pass",
        "A hard moment, described plainly and accurately. Sensitive is not wrong.",
        {
            "category": "story",
            "kind": "moment",
            "title": "The night you were late",
            "headline": "September 18, 2021: a missed 8pm, then silence",
            "body": 'Alex wrote "you said you\'d be there at 8"; your text had come at 9:40.'
            " It went quiet until you wrote \"i'm sorry about saturday. i was wrong and"
            ' i hate not talking to you".',
            "evidence": [
                _ev("001485", "you said you'd be there at 8"),
                _ev("001487", "you texted at 9:40"),
                _ev("001491", "i'm sorry about saturday. i was wrong and i hate not talking"),
            ],
        },
    ),
    Case(
        "wrong_sender",
        "revise",
        "Alex said it first, not you.",
        _ily("You said it first: May 14, 2021, at 11:48pm", "Alex answered 41 seconds later."),
        accept=("reject",),
    ),
    Case(
        "wrong_date",
        "revise",
        "It was May 14 (Alex's local time), not June 3.",
        _ily("Alex said it first, on June 3, 2021"),
        accept=("reject",),
    ),
    Case(
        "sarcasm",
        "reject",
        '"very glamorous" about eating sushi over the sink is self-deprecating.',
        {
            "category": "free",
            "kind": "moment",
            "title": "Alex's glamorous life",
            "headline": "Alex's dinners are pure glamour",
            "body": 'Sushi for dinner, and in Alex\'s own words: "very glamorous".',
            "evidence": [_ev("000167", "ate sushi standing over the sink, very glamorous")],
        },
        accept=("revise",),
    ),
    Case(
        "playlist_decoy",
        "reject",
        'A sloppy pattern finds "i love your playlist taste", which is not "I love you".',
        {
            "category": "milestone",
            "kind": "first_occurrence",
            "title": 'The first "I love you"',
            "headline": "You said it first: March 20, 2021",
            "body": 'Alex\'s reply: "unfortunately??"',
            "evidence": [
                _ev("000171", "ok i love your playlist taste, unfortunately"),
                _ev("000172", "unfortunately??"),
            ],
            "first": {"pattern": "i love you", "sender": "any"},
        },
    ),
    Case(
        "group_chat_trap",
        "reject",
        'The April 2 "love you" was Jules in the group chat; the 1:1 cites show none.',
        {
            "category": "milestone",
            "kind": "moment",
            "title": "Love, early",
            "headline": 'Alex said "love you" on April 2, 2021, the night after trivia',
            "body": "Weeks before the big one, it was already in the air.",
            "evidence": [_ev("000217", "ok so!!"), _ev("000218", "sounds good")],
        },
        accept=("revise",),
    ),
    Case(
        "goose_misattributed",
        "revise",
        "Alex coined it; your message is the echo.",
        {
            "category": "language",
            "kind": "inside_joke",
            "title": "The goose incident",
            "headline": 'You named it "the goose incident"',
            "body": "After Alex's run-in with a goose, you declared it would be remembered.",
            "evidence": [_ev("000228", "the goose incident will be remembered")],
        },
        accept=("reject",),
    ),
    Case(
        "number_without_metric",
        "revise",
        "22 is the true count, but nothing in the card measures it.",
        {
            "category": "language",
            "kind": "moment",
            "title": "Love you, goodnight",
            "headline": 'You two have said "love you" 22 times, mostly at bedtime',
            "body": 'It usually comes with a goodnight, like "ok bed for real love you".',
            "evidence": [_ev("000725", "ok bed for real love you")],
        },
    ),
    Case(
        "bland_plant",
        "pass",
        "True and harmless, just not interesting: should pass with low delight.",
        {
            "category": "free",
            "kind": "moment",
            "title": "Plant news",
            "headline": "Alex told you their plant was dying",
            "body": "",
            "evidence": [_ev("000502", "my plant is dying!!")],
        },
    ),
]


def prepare(ctx: ToolContext, case: Case) -> Insight:
    """The case as an Insight that passed the deterministic checks (raises otherwise)."""
    raw = dict(case.insight)
    if case.count:
        out = count(ctx, pattern=case.count)
        raw["metric"] = {"query_id": out["query_id"], "path": "total", "value": out["total"]}
    draft = InsightDraft.model_validate(raw)
    failures = checks.check(ctx, draft)
    if failures:
        raise ValueError(f"{case.id} fails the deterministic checks: {failures}")
    ins = Insight(id=f"cal_{case.id}", **draft.model_dump())
    ins.verification.deterministic = "pass"
    return ins


def open_sample(store_dir: Path) -> tuple[Store, int]:
    from sincehello.connectors.synthetic import SyntheticConnector

    store = Store(store_dir / "calibration.db")
    SyntheticConnector().ingest(store)
    return store, store.person_for_handle("synthetic", "+12125550142")


def run(critic: Any, max_cost: float = 0.50, log: Any = None) -> dict[str, Any]:
    """Run every case through `critic` once, stopping when the budget runs out."""
    budget = Budget(max_cost)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        store, pid = open_sample(Path(tmp))
        try:
            ctx = ToolContext(store, pid, "America/New_York")
            ctx.budget, ctx.log = budget, log
            for case in CASES:
                ins = prepare(ctx, case)
                try:
                    v: Verdict | None = critic(ins, ctx)
                    error = None
                except CriticUnavailable as e:
                    v, error = None, str(e)
                rows.append(
                    {
                        "case": case.id,
                        "label": case.label,
                        "verdict": v.verdict if v else None,
                        "agree": bool(v and v.verdict == case.label),
                        "acceptable": bool(v and v.verdict in case.ok),
                        "scores": v.scores.model_dump() if v and v.scores else None,
                        "issues": v.issues if v else [],
                        "error": error,
                    }
                )
        finally:
            store.close()
    judged = [r for r in rows if r["verdict"]]
    n = len(judged)

    def binary(r: dict[str, Any]) -> bool:  # pass vs. not pass
        return (r["verdict"] == "pass") == (r["label"] == "pass")

    return {
        "cases": rows,
        "judged": n,
        "agreement": round(sum(r["agree"] for r in judged) / n, 3) if n else None,
        "acceptable": round(sum(r["acceptable"] for r in judged) / n, 3) if n else None,
        "pass_vs_flag": round(sum(map(binary, judged)) / n, 3) if n else None,
        "cost_usd": round(budget.spent, 4),
        "calls": budget.critic_calls,
    }
