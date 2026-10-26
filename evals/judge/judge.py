"""The eval judge (SPEC.md §10.2). Not part of the pipeline: it only scores its output.

Two modes, each a one-message request with its own byte-stable system prompt (the cached
prefix, like `verify/critic.md`):

- **Per insight** (`judge.md`): the card, its metric with the query behind it, and every
  cited message with the critic's context window around it. Returns `{notes, scores,
  keep}` with five 1-5 scores (`DIMENSIONS`). It is a separate prompt and rubric from the
  critic (§7.6) and never sees the critic's verdict or scores, so the eval doesn't share
  the critic's blind spots or grade the pipeline against itself.
- **Pairwise** (`pairwise.md`): two decks of cards (no excerpts), shown in random order
  as `deck_1` / `deck_2`. Returns `{reasons, winner}`; `compare` maps it back to a / b.

Every call is guarded by and charged to one `Budget` (the "critic" lane: one-shot calls
with their own estimate), so a command's `--max-cost` is a hard cap. A call that can't
give a result (refusal, malformed output twice, API error, no budget) raises
`JudgeUnavailable`; the caller records it and moves on.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib import resources
from statistics import mean
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from sincehello.agent.budget import Budget
from sincehello.agent.context import ToolContext, ToolError
from sincehello.agent.loop import FALLBACK_BETA, send
from sincehello.report.schema import Insight
from sincehello.verify.critic import _excerpts

DIMENSIONS = ("accuracy", "specificity", "delight", "tone", "non_obviousness")
MAX_ATTEMPTS = 2  # a malformed response is retried once
EGRESS_TOOL = "judge"
# A card at or below this judge accuracy counts toward the hallucination rate (§10.1).
HALLUCINATION_AT = 2

_SCORE = {"type": "integer", "enum": [1, 2, 3, 4, 5]}  # no minimum/maximum in strict schemas
SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Notes first so the scores are written after the reasons for them.
        "notes": {"type": "array", "items": {"type": "string"}},
        "scores": {
            "type": "object",
            "properties": {k: _SCORE for k in DIMENSIONS},
            "required": list(DIMENSIONS),
            "additionalProperties": False,
        },
        "keep": {"type": "boolean"},
    },
    "required": ["notes", "scores", "keep"],
    "additionalProperties": False,
}
PAIRWISE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasons": {"type": "string"},
        "winner": {"type": "string", "enum": ["deck_1", "deck_2", "tie"]},
    },
    "required": ["reasons", "winner"],
    "additionalProperties": False,
}


class JudgeUnavailable(Exception):
    """The judge couldn't score this item; the message says why."""


@dataclass
class JudgeConfig:
    model: str = "claude-opus-5-5"
    # Medium, like the critic: a bounded reading task per card. Raise it if calibration
    # against human ratings shows the judge missing misreadings.
    effort: str = "medium"
    max_tokens: int = 16_000  # thinking included; the JSON itself is small
    fallbacks: bool = True


@cache
def prompt(name: str) -> str:
    """`judge` or `pairwise`: the whole system prompt for that mode."""
    return resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")


def prompt_version() -> str:
    """A short hash of both prompts, recorded with results so versions can be compared."""
    both = prompt("judge") + "\0" + prompt("pairwise")
    return hashlib.sha256(both.encode()).hexdigest()[:12]


def build_request(config: JudgeConfig, mode: str) -> dict[str, Any]:
    """Everything but `messages`: identical on every call of a mode, so the prefix caches."""
    schema = SCORE_SCHEMA if mode == "judge" else PAIRWISE_SCHEMA
    request: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": [{"type": "text", "text": prompt(mode), "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        "betas": [],
    }
    if config.fallbacks:
        request["fallbacks"] = "default"
        request["betas"] = [FALLBACK_BETA]
    return request


# --- the per-insight message -------------------------------------------------------


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def people(ctx: ToolContext) -> dict[str, str | None]:
    me = ctx.store.conn.execute("SELECT display_name FROM person WHERE id = 1").fetchone()
    return {"me": me["display_name"] if me else None, "them": ctx.person_name}


def _metric(ins: Insight, queries: dict[str, dict[str, Any]]) -> dict[str, Any] | str:
    if ins.metric is None:
        return "none"
    q = queries.get(ins.metric.query_id)
    return {
        "query_id": ins.metric.query_id,
        "query": {"kind": q.get("kind"), "args": q.get("args")} if q else "unknown query",
        "path": ins.metric.path,
        "value": ins.metric.value,
    }


def render_card(ins: Insight, ctx: ToolContext, queries: dict[str, dict[str, Any]]) -> str:
    """The user message for one judge call. Logs every message shown to the egress log.

    Only the card itself goes in: no id, scores or verification, so the judge can't
    anchor on the critic.
    """
    card = ins.model_dump(
        include={"category", "kind", "title", "headline", "body", "evidence", "first"},
        exclude_none=True,
    )
    cited = {ev.message_id for ev in ins.evidence}
    present = [ev for ev in ins.evidence if ev.message_id in ctx.by_id]
    shown = ins.model_copy(update={"evidence": present})
    lines = []
    for m in _excerpts(shown, ctx) if present else []:
        if m is None:
            lines.append("…")
            continue
        row = ctx.show(m, EGRESS_TOOL)
        if m.id in cited:
            row["cited"] = True
        lines.append(_json(row))
    missing = sorted(cited - {ev.message_id for ev in present})
    if missing:
        lines.append(_json({"missing_from_store": missing}))
    metric = _metric(ins, queries)
    return "\n\n".join(
        [
            f"<people>{_json(people(ctx))}</people>",
            f"<card>{_json(card)}</card>",
            f"<metric>{metric if isinstance(metric, str) else _json(metric)}</metric>",
            "<conversation>\n" + "\n".join(lines) + "\n</conversation>",
            "Score this card.",
        ]
    )


def deck_cards(insights: list[Insight]) -> list[dict[str, Any]]:
    """Cards as a reader sees them, for the pairwise prompt."""
    return [
        {
            "title": i.title,
            "headline": i.headline,
            "body": i.body,
            "quotes": [ev.quote for ev in i.evidence],
        }
        for i in insights
    ]


def render_pair(who: dict[str, Any], first: list[Insight], second: list[Insight]) -> str:
    return "\n\n".join(
        [
            f"<people>{_json(who)}</people>",
            f"<deck_1>{_json(deck_cards(first))}</deck_1>",
            f"<deck_2>{_json(deck_cards(second))}</deck_2>",
            "Which deck is the better gift?",
        ]
    )


# --- responses ---------------------------------------------------------------------


class _Scores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accuracy: Literal[1, 2, 3, 4, 5]
    specificity: Literal[1, 2, 3, 4, 5]
    delight: Literal[1, 2, 3, 4, 5]
    tone: Literal[1, 2, 3, 4, 5]
    non_obviousness: Literal[1, 2, 3, 4, 5]


class _ScoreOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: list[str]
    scores: _Scores
    keep: bool


class _PairOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reasons: str
    winner: Literal["deck_1", "deck_2", "tie"]


@dataclass(frozen=True)
class Judgment:
    notes: list[str]
    scores: dict[str, int]
    keep: bool

    def to_dict(self) -> dict[str, Any]:
        return {"notes": self.notes, "scores": self.scores, "keep": self.keep}


@dataclass(frozen=True)
class PairwiseResult:
    winner: str  # "a" | "b" | "tie"
    shown_first: str  # "a" | "b": which run was deck_1
    reasons: str

    def to_dict(self) -> dict[str, Any]:
        return {"winner": self.winner, "shown_first": self.shown_first, "reasons": self.reasons}


def _answer_text(response: Any) -> str:
    if response.stop_reason == "max_tokens":
        raise ValueError("the response hit max_tokens before the JSON was complete")
    blocks = list(response.content)
    # After a mid-response fallback, only what follows the last switch is the answer.
    last = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    return "".join(b.text for b in blocks[last + 1 :] if b.type == "text")


def parse_judgment(response: Any) -> Judgment:
    try:
        out = _ScoreOut.model_validate_json(_answer_text(response))
    except ValidationError as e:
        raise ValueError(f"output doesn't match the schema: {e.errors()[0]['msg']}") from None
    notes = [n.strip() for n in out.notes if n.strip()]
    return Judgment(notes, out.scores.model_dump(), out.keep)


def parse_pair(response: Any) -> _PairOut:
    try:
        return _PairOut.model_validate_json(_answer_text(response))
    except ValidationError as e:
        raise ValueError(f"output doesn't match the schema: {e.errors()[0]['msg']}") from None


# --- the judge ---------------------------------------------------------------------


Log = Callable[..., None]


class LLMJudge:
    """Scores cards and compares decks, spending from one `Budget`."""

    def __init__(
        self,
        client: Any,
        budget: Budget,
        config: JudgeConfig | None = None,
        log: Log | None = None,
    ) -> None:
        self.client = client
        self.budget = budget
        self.config = config or JudgeConfig()
        self.log = log or (lambda *_a, **_k: None)
        self.requests = {m: build_request(self.config, m) for m in ("judge", "pairwise")}
        self._logged: set[str] = set()

    def _call(self, mode: str, content: str, parse: Callable[[Any], Any], **tag: Any) -> Any:
        request = self.requests[mode]
        if mode not in self._logged:
            self.log("judge_request_static", mode=mode, request=request)
            self._logged.add(mode)
        messages = [{"role": "user", "content": content}]
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not self.budget.can_afford_critic():
                self.log("judge_skipped", mode=mode, **tag, spent=self.budget.spent)
                raise JudgeUnavailable("not enough budget left for another judge call")
            if attempt == 1:
                self.log("judge_request", mode=mode, **tag, messages=messages)
            try:
                response = send(self.client, request, messages)
            except anthropic.APIError as e:
                self.log("judge_api_error", mode=mode, **tag, error=str(e))
                raise JudgeUnavailable(f"API error ({type(e).__name__})") from None
            cost = self.budget.add(response, kind="critic")
            self.log(
                "judge_response",
                mode=mode,
                **tag,
                attempt=attempt,
                response=response,
                cost_usd=cost,
                total=self.budget.spent,
            )
            if response.stop_reason == "refusal":
                category = getattr(response.stop_details, "category", None)
                self.log("judge_refusal", mode=mode, **tag, category=category)
                raise JudgeUnavailable(f"the judge model declined ({category})")
            try:
                return parse(response)
            except ValueError as e:
                problem = str(e)
                self.log("judge_malformed", mode=mode, **tag, attempt=attempt, problem=problem)
        raise JudgeUnavailable(f"malformed judge output after {MAX_ATTEMPTS} tries: {problem}")

    def score(
        self, ins: Insight, ctx: ToolContext, queries: dict[str, dict[str, Any]] | None = None
    ) -> Judgment:
        if not self.budget.can_afford_critic():  # before rendering, which logs egress
            raise JudgeUnavailable("not enough budget left for another judge call")
        content = render_card(ins, ctx, queries if queries is not None else ctx.queries)
        judgment = self._call("judge", content, parse_judgment, insight=ins.id)
        self.log("judge_scores", insight=ins.id, **judgment.to_dict())
        return judgment

    def compare(
        self,
        who: dict[str, Any],
        deck_a: list[Insight],
        deck_b: list[Insight],
        rng: random.Random | None = None,
    ) -> PairwiseResult:
        """Which deck is better, with the order shown to the judge randomized."""
        a_first = (rng or random.Random()).random() < 0.5
        first, second = (deck_a, deck_b) if a_first else (deck_b, deck_a)
        out = self._call("pairwise", render_pair(who, first, second), parse_pair)
        to_run = {"deck_1": "a" if a_first else "b", "deck_2": "b" if a_first else "a"}
        result = PairwiseResult(
            to_run.get(out.winner, "tie"), "a" if a_first else "b", out.reasons.strip()
        )
        self.log("judge_pairwise", **result.to_dict())
        return result


def judge_insights(
    judge: LLMJudge,
    insights: list[Insight],
    ctx: ToolContext,
    queries: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score every insight once; the rows plus run-level means (SPEC.md §10.2)."""
    rows = []
    for ins in insights:
        try:
            j: Judgment | None = judge.score(ins, ctx, queries)
            error = None
        except (JudgeUnavailable, ToolError) as e:
            j, error = None, str(e)
        rows.append(
            {
                "insight": ins.id,
                "title": ins.title,
                "critic": ins.verification.critic,
                **(j.to_dict() if j else {"notes": [], "scores": None, "keep": None}),
                "error": error,
            }
        )
    return {
        "judge": {
            "model": judge.config.model,
            "effort": judge.config.effort,
            "prompt_version": prompt_version(),
        },
        "insights": rows,
        **summarize(rows),
        "cost_usd": round(judge.budget.spent, 4),
        "calls": judge.budget.critic_calls,
        "egress": {
            "messages_shown": len(
                {e["message_id"] for e in ctx.egress.entries if e["tool"] == EGRESS_TOOL}
            )
        },
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Means per dimension, keep rate and hallucination rate over the scored rows."""
    scored = [r for r in rows if r.get("scores")]
    if not scored:
        return {"scored": 0, "means": None, "keep_rate": None, "hallucination_rate": None}
    means = {d: round(mean(r["scores"][d] for r in scored), 2) for d in DIMENSIONS}
    means["overall"] = round(mean(means[d] for d in DIMENSIONS), 2)
    n = len(scored)
    return {
        "scored": n,
        "means": means,
        "keep_rate": round(sum(bool(r["keep"]) for r in scored) / n, 3),
        "hallucination_rate": round(
            sum(r["scores"]["accuracy"] <= HALLUCINATION_AT for r in scored) / n, 3
        ),
    }
