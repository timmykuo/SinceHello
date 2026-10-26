"""LOOP 2 step 2: the LLM critic (SPEC.md §7.6).

`LLMCritic` plugs into `ToolContext.critic` (the `tools.Critic` hook) and judges one
insight that already passed the deterministic checks. Each call is its own one-message
request: it never sees the agent's conversation, only

- the insight (title, headline, body, evidence, `first`, era),
- its metric, with the query's arguments and the value recomputed by code,
- every cited message with `CONTEXT_EACH_SIDE` messages around it, read through
  `ToolContext.around` / `ToolContext.show` like `get_context`, so they land in the
  egress log (tool "critic"),
- on a resubmission, its own notes from the previous round.

It returns `{issues, scores{accuracy, specificity, delight, tone}, verdict}` through
structured outputs (`output_config.format`). Forced tool use isn't available on Opus 5.5.

Request layout: the system prompt (`critic.md`: role, procedure, tone, rubric) and the
output schema never change, and the system block carries the cache breakpoint. All
per-insight text is in the user message after it.

Costs go to the run's `Budget` as kind "critic" (`ctx.budget`), and every request,
response and verdict goes to the run log as `critic_*` events (`ctx.log`), apart from the
agent's `request`/`response` events. When no verdict is possible (a refusal from the
whole fallback chain, malformed output twice, an API error, or no budget left) it raises
`CriticUnavailable`, and `propose_insight` keeps the insight as unverified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sincehello.agent.budget import response_cost
from sincehello.agent.context import Message, ToolContext, ToolError
from sincehello.agent.loop import FALLBACK_BETA, send
from sincehello.agent.tools import CriticUnavailable, Verdict
from sincehello.report.schema import Insight, Scores
from sincehello.verify.checks import at_path

CONTEXT_EACH_SIDE = 5
MAX_ATTEMPTS = 2  # a malformed response is retried once
EGRESS_TOOL = "critic"

_SCORE = {"type": "integer", "enum": [1, 2, 3, 4, 5]}  # no minimum/maximum in strict schemas
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Issues come first so the verdict is written after the reasons for it.
        "issues": {"type": "array", "items": {"type": "string"}},
        "scores": {
            "type": "object",
            "properties": {k: _SCORE for k in ("accuracy", "specificity", "delight", "tone")},
            "required": ["accuracy", "specificity", "delight", "tone"],
            "additionalProperties": False,
        },
        "verdict": {"type": "string", "enum": ["pass", "revise", "reject"]},
    },
    "required": ["issues", "scores", "verdict"],
    "additionalProperties": False,
}


@dataclass
class CriticConfig:
    model: str = "claude-opus-5-5"
    # Medium: each call is a bounded reading task over ~10-40 short messages, not an
    # open-ended search, and a run makes ~20-40 of them. Medium is enough to read tone
    # and attribution carefully at a fraction of high's thinking tokens. Tune it with
    # the calibration set (evals/critic_calibration.py).
    effort: str = "medium"
    max_tokens: int = 16_000  # thinking included; the JSON itself is small
    fallbacks: bool = True


@cache
def system_prompt() -> str:
    return resources.files(__package__).joinpath("critic.md").read_text(encoding="utf-8")


def build_request(config: CriticConfig) -> dict[str, Any]:
    """Everything but `messages`: identical on every call, so the system prompt caches."""
    request: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": [
            {"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}
        ],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        "betas": [],
    }
    if config.fallbacks:
        request["fallbacks"] = "default"
        request["betas"] = [FALLBACK_BETA]
    return request


# --- the per-insight message -------------------------------------------------------


def _people(ctx: ToolContext) -> dict[str, str | None]:
    me = ctx.store.conn.execute("SELECT display_name FROM person WHERE id = 1").fetchone()
    return {"me": me["display_name"] if me else None, "them": ctx.person_name}


def _metric(ins: Insight, ctx: ToolContext) -> dict[str, Any] | None:
    if ins.metric is None:
        return None
    q = ctx.queries.get(ins.metric.query_id, {})
    out: dict[str, Any] = {
        "query_id": ins.metric.query_id,
        "query": {"kind": q.get("kind"), "args": q.get("args")},
        "path": ins.metric.path,
        "claimed": ins.metric.value,
    }
    try:
        out["recomputed"] = at_path(ctx.recompute(ins.metric.query_id), ins.metric.path)
    except ToolError as e:  # can't happen after the deterministic checks; say so anyway
        out["recomputed"] = f"error: {e}"
    return out


def _excerpts(ins: Insight, ctx: ToolContext) -> list[Message | None]:
    """Cited messages with their neighbours, merged in history order; None marks a gap."""
    picked: set[int] = set()
    for ev in ins.evidence:
        for m in ctx.around(ev.message_id, CONTEXT_EACH_SIDE, CONTEXT_EACH_SIDE):
            picked.add(ctx.by_id[m.id])
    out: list[Message | None] = []
    prev: Message | None = None
    for i in sorted(picked):
        m = ctx.messages[i]
        if prev is not None and (
            m.conversation_id != prev.conversation_id or i != ctx.by_id[prev.id] + 1
        ):
            out.append(None)
        out.append(m)
        prev = m
    return out


def render_review(ins: Insight, ctx: ToolContext) -> str:
    """The user message for one critic call. Logs every message shown to the egress log."""
    card = ins.model_dump(
        include={"category", "kind", "title", "headline", "body", "evidence", "first", "era_id"},
        exclude_none=True,
    )
    cited = {ev.message_id for ev in ins.evidence}
    lines = []
    for m in _excerpts(ins, ctx):
        if m is None:
            lines.append("…")
            continue
        shown = ctx.show(m, EGRESS_TOOL)
        if m.id in cited:
            shown["cited"] = True
        lines.append(_json(shown))
    metric = _metric(ins, ctx)
    parts = [
        f"<people>{_json(_people(ctx))}</people>",
        f"<insight>{_json(card)}</insight>",
        f"<metric>{_json(metric) if metric else 'none'}</metric>",
        "<conversation>\n" + "\n".join(lines) + "\n</conversation>",
    ]
    if ins.verification.revisions:
        notes = "\n".join(f"- {n}" for n in ins.verification.critic_notes) or "- (none)"
        parts.append(
            f"<previous_review>Revision {ins.verification.revisions}. Your issues with the"
            f" previous version:\n{notes}\n</previous_review>"
        )
    parts.append("Review this insight.")
    return "\n\n".join(parts)


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# --- the response ------------------------------------------------------------------


class _Scores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accuracy: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    delight: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues: list[str]
    scores: _Scores
    verdict: Literal["pass", "revise", "reject"]


def parse_verdict(response: Any) -> Verdict:
    """The verdict in a critic response; ValueError if it is cut off or malformed."""
    if response.stop_reason == "max_tokens":
        raise ValueError("the response hit max_tokens before the JSON was complete")
    blocks = list(response.content)
    # After a mid-response fallback, only what follows the last switch is the answer.
    last = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    text = "".join(b.text for b in blocks[last + 1 :] if b.type == "text")
    try:
        out = _Output.model_validate_json(text)
    except ValidationError as e:
        raise ValueError(f"output doesn't match the schema: {e.errors()[0]['msg']}") from None
    issues = [i.strip() for i in out.issues if i.strip()]
    if out.verdict != "pass" and not issues:
        raise ValueError(f"a {out.verdict!r} verdict came with no issues")
    return Verdict(out.verdict, issues, Scores(**out.scores.model_dump()))


# --- the critic --------------------------------------------------------------------


class LLMCritic:
    """`tools.Critic` backed by Claude. One instance per run (it logs its request once)."""

    def __init__(self, client: Any, config: CriticConfig | None = None) -> None:
        self.client = client
        self.config = config or CriticConfig()
        self.request = build_request(self.config)
        self._logged_static = False

    def __call__(self, insight: Insight, ctx: ToolContext) -> Verdict:
        log = ctx.log or (lambda *_a, **_k: None)
        budget = ctx.budget
        if not self._logged_static:
            log("critic_request_static", request=self.request)
            self._logged_static = True
        tag = {"turn": ctx.turn, "revision": insight.verification.revisions}

        problem = ""
        messages: list[dict[str, Any]] | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if budget is not None and not budget.can_afford_critic():
                log("critic_skipped", **tag, reason="budget", spent=budget.spent)
                raise CriticUnavailable("not enough budget left for a critic call")
            if messages is None:  # built after the budget check: rendering logs egress
                messages = [{"role": "user", "content": render_review(insight, ctx)}]
                log("critic_request", **tag, title=insight.title, messages=messages)
            try:
                response = send(self.client, self.request, messages)
            except anthropic.APIError as e:
                log("critic_api_error", **tag, error=str(e), type=type(e).__name__)
                raise CriticUnavailable(f"API error ({type(e).__name__})") from None
            if budget is not None:
                cost = budget.add(response, kind="critic")
                total = budget.spent
            else:
                cost = total = response_cost(response)[0]
            log(
                "critic_response",
                **tag,
                attempt=attempt,
                response=response,
                cost_usd=cost,
                total=total,
            )

            if response.stop_reason == "refusal":
                # Server-side fallbacks already tried the other models; don't retry.
                category = getattr(response.stop_details, "category", None)
                log("critic_refusal", **tag, category=category)
                raise CriticUnavailable(f"the critic model declined to review it ({category})")
            try:
                verdict = parse_verdict(response)
            except ValueError as e:
                problem = str(e)
                log("critic_malformed", **tag, attempt=attempt, problem=problem)
                continue
            log(
                "critic_verdict",
                **tag,
                verdict=verdict.verdict,
                issues=verdict.issues,
                scores=verdict.scores.model_dump() if verdict.scores else None,
            )
            return verdict
        raise CriticUnavailable(f"malformed critic output after {MAX_ATTEMPTS} tries: {problem}")
