"""The report.json contract (SPEC.md §8.1), as pydantic models.

`InsightDraft` is what the agent proposes through `propose_insight`. The pipeline adds
`id`, `scores` and `verification` to make an `Insight`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Category = Literal["milestone", "language", "rhythm", "story", "free"]
Sender = Literal["me", "them", "any"]

# Kinds whose claim is a number or a date, so they must carry a metric (SPEC.md §7.6).
NUMERIC_KINDS = frozenset({"stat", "count"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    message_id: str = Field(min_length=3)
    quote: str = Field(min_length=1, max_length=400)


class Metric(_Strict):
    """A number or date the insight states, tied to a recomputable query.

    `path` points into the query's value (dotted, list indices allowed, e.g.
    "by_sender.them" or "top.0.date"); `value` is the claimed value at that path.
    """

    query_id: str = Field(min_length=3)
    path: str = ""
    value: Any = None


class FirstClaim(_Strict):
    """The pattern behind a "first time ..." insight, so it can be re-run."""

    pattern: str = Field(min_length=1, max_length=200)
    sender: Sender = "any"


class InsightDraft(_Strict):
    category: Category
    kind: str = Field(min_length=2, max_length=40)
    title: str = Field(min_length=3, max_length=100)
    headline: str = Field(min_length=3, max_length=200)
    body: str = Field(max_length=800)
    evidence: list[Evidence] = Field(min_length=1, max_length=8)
    metric: Metric | None = None
    era_id: str | None = None
    first: FirstClaim | None = None

    @field_validator("era_id")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _kind_requirements(self) -> InsightDraft:
        if self.kind == "first_occurrence" and self.first is None:
            raise ValueError("kind 'first_occurrence' needs `first` (the pattern to re-check)")
        if self.kind != "first_occurrence":
            # Only "first time" claims are re-checked; on other kinds a `first` is noise
            # the agent may fill to satisfy the schema, so it must not gate the insight.
            self.first = None
        if (self.kind in NUMERIC_KINDS or self.category == "rhythm") and self.metric is None:
            raise ValueError(
                f"a {self.category}/{self.kind} insight states a number, so it needs `metric`"
                " with a query_id from get_stats, count or first_occurrence"
            )
        return self


class Scores(_Strict):
    accuracy: int | None = None
    specificity: int | None = None
    delight: int | None = None
    tone: int | None = None


class Verification(_Strict):
    deterministic: Literal["pass", "fail", "pending"] = "pending"
    # SPEC.md §7.6 step 2. "pending" when no critic ran (--no-critic) or it couldn't give
    # a verdict (refusal, malformed output, budget); curation keeps only "pass".
    critic: Literal["pass", "revise", "reject", "pending"] = "pending"
    revisions: int = 0  # critic revise rounds before this version passed
    critic_notes: list[str] = Field(default_factory=list)  # the critic's latest notes


class Insight(InsightDraft):
    id: str
    scores: Scores = Field(default_factory=Scores)
    verification: Verification = Field(default_factory=Verification)
    # The earliest cited message's time (ISO UTC), set when report.json is written, so
    # curation can date milestones without the store (SPEC.md §7.7).
    at_utc: str | None = None


class RunInfo(_Strict):
    run_id: str
    status: str
    cost_usd: float
    tokens: dict[str, int]
    turns: int
    messages_shared: int
    pct_shared: float
    model: str
    effort: str
    version: str
    critic_calls: int = 0  # included in cost_usd and tokens
    critic_cost_usd: float = 0.0


class Curation(_Strict):
    """How the deck was built (SPEC.md §7.7), so a reader can see what was left out."""

    verified: bool  # every card passed the critic; False when no critic ran (--no-critic)
    min_cards: int
    max_cards: int
    scores: dict[str, float]  # curation score of each eligible insight
    dropped: dict[str, str]  # insight id -> why it isn't in the deck


class Report(_Strict):
    person: dict[str, Any]
    range: dict[str, str | None]
    sources: list[str]
    deck: list[str]  # ordered insight ids, filled by curation (sincehello/curate)
    insights: list[Insight]
    eras: list[dict[str, Any]]
    stats: dict[str, Any]
    summary: str | None
    run: RunInfo
    curation: Curation | None = None
