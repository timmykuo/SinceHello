"""Naming inferred timezone periods (SPEC.md §6.1, step 3).

`tz_infer` finds when someone was on a different clock but not where. `TzNamer` asks
Claude once per suggestion: it reads the 1:1 messages around the period's start and end
(`CONTEXT_EACH_SIDE` hours each side) plus travel mentions within `KEYWORD_DAYS` of
either boundary, and returns `{notes, place, iana_tz, evidence_message_id, quote}`
through structured outputs. Code then checks the answer: the zone must exist and fit
the inferred offset during the period, and the quote must come from a message it was
shown; anything that fails is dropped rather than trusted.

Request layout follows the critic (§7.6): the system prompt (`prompts/tz_namer.md`) and
output schema never change and carry the cache breakpoint; the period and messages go in
the one user message. Messages go out through `ToolContext.show` under egress tool
`tz_namer`. Costs go to a `Budget` (the one-shot "critic" lane), so the command's
`--max-cost` is a hard cap.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from sincehello.agent import prompts
from sincehello.agent.budget import Budget
from sincehello.agent.context import Message, ToolContext, iso
from sincehello.agent.loop import FALLBACK_BETA, send
from sincehello.analysis.tz_infer import TRAVEL, Suggestion, midpoint_ms, offset_label, zone_matches
from sincehello.store.store import ME_PERSON_ID

HOUR_MS = 3_600_000
CONTEXT_EACH_SIDE = 36  # hours of messages around each boundary
MAX_PER_BOUNDARY = 30
KEYWORD_DAYS = 7
MAX_KEYWORD = 12
MAX_ATTEMPTS = 2  # a malformed response is retried once
EGRESS_TOOL = "tz_namer"
OFFSET_TOLERANCE_H = 1.5

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        k: {"type": "string"} for k in ("notes", "place", "iana_tz", "evidence_message_id", "quote")
    },
    "required": ["notes", "place", "iana_tz", "evidence_message_id", "quote"],
    "additionalProperties": False,
}


@dataclass
class NamerConfig:
    model: str = "claude-opus-5-5"
    effort: str = "low"  # a short reading task over ~60 messages
    max_tokens: int = 8_000
    fallbacks: bool = True


@dataclass
class Naming:
    place: str | None
    iana_tz: str | None
    evidence_message_id: str | None
    quote: str | None
    notes: str
    dropped: list[str]  # parts of the answer that failed the checks, and why


class NameUnavailable(Exception):
    """No usable answer: a refusal, malformed output twice, an API error, or no budget."""


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str
    place: str
    iana_tz: str
    evidence_message_id: str
    quote: str


def build_request(config: NamerConfig) -> dict[str, Any]:
    """Everything but `messages`: identical on every call, so the system prompt caches."""
    request: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": [
            {
                "type": "text",
                "text": prompts.load("tz_namer"),
                "cache_control": {"type": "ephemeral"},
            }
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


def _picked(sg: Suggestion, ctx: ToolContext) -> list[Message]:
    """Messages near each boundary, plus travel mentions near either, in time order."""
    chosen: dict[int, Message] = {}
    for boundary in (sg.start_utc, sg.end_utc):
        if boundary is None:
            continue
        near = ctx.window(
            boundary - CONTEXT_EACH_SIDE * HOUR_MS, boundary + CONTEXT_EACH_SIDE * HOUR_MS
        )
        near = sorted(near, key=lambda m: abs(m.sent_at_utc - boundary))[:MAX_PER_BOUNDARY]
        travel = [
            m
            for m in ctx.window(
                boundary - KEYWORD_DAYS * 24 * HOUR_MS, boundary + KEYWORD_DAYS * 24 * HOUR_MS
            )
            if TRAVEL.search(m.text)
        ]
        travel = sorted(travel, key=lambda m: abs(m.sent_at_utc - boundary))[: MAX_KEYWORD // 2]
        for m in [*near, *travel]:
            chosen[m.rowid] = m
    return sorted(chosen.values(), key=lambda m: (m.sent_at_utc, m.rowid))


def render(sg: Suggestion, ctx: ToolContext) -> tuple[str, dict[str, Message]]:
    """The user message for one suggestion, and the messages it showed by id."""
    me = ctx.store.conn.execute("SELECT display_name FROM person WHERE id = ?", (ME_PERSON_ID,))
    row = me.fetchone()
    people = {"me": row["display_name"] if row else None, "them": ctx.person_name}
    period = {
        "who": "me" if sg.person_id == ME_PERSON_ID else "them",
        "start_utc": iso(sg.start_utc),
        "end_utc": iso(sg.end_utc) if sg.end_utc is not None else "still there at the end",
        "inferred_utc_offset": offset_label(sg.utc_offset_hours),
    }
    shown: dict[str, Message] = {}
    lines = []
    for m in _picked(sg, ctx):
        shown[m.id] = m
        lines.append(
            json.dumps(ctx.show(m, EGRESS_TOOL), ensure_ascii=False, separators=(",", ":"))
        )
    text = "\n\n".join(
        [
            f"<people>{json.dumps(people, ensure_ascii=False)}</people>",
            f"<period>{json.dumps(period)}</period>",
            "<messages>\n" + "\n".join(lines) + "\n</messages>",
            "Name the place for this period.",
        ]
    )
    return text, shown


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def check(out: _Output, sg: Suggestion, shown: dict[str, Message], last_ms: int) -> Naming:
    """Keep only the parts of the answer that hold up."""
    dropped = []
    place = out.place.strip() or None
    tz = out.iana_tz.strip() or None
    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            dropped.append(f"iana_tz {tz!r} is not a timezone")
            tz = None
    if tz is not None and not zone_matches(
        tz, sg.utc_offset_hours, midpoint_ms(sg, last_ms), OFFSET_TOLERANCE_H
    ):
        dropped.append(f"{tz} doesn't fit the inferred {offset_label(sg.utc_offset_hours)}")
        tz = place = None
    if place is not None and tz is None:
        dropped.append(f"place {place!r} came without a usable timezone")
        place = None
    mid = out.evidence_message_id.strip() or None
    quote = out.quote.strip() or None
    if mid is not None and (
        mid not in shown or not quote or _squash(quote) not in _squash(shown[mid].text)
    ):
        dropped.append(f"evidence {mid!r} isn't a shown message containing the quote")
        mid = quote = None
    return Naming(place, tz, mid, quote if mid else None, out.notes.strip(), dropped)


def _parse(response: Any) -> _Output:
    if response.stop_reason == "max_tokens":
        raise ValueError("the response hit max_tokens before the JSON was complete")
    blocks = list(response.content)
    last = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    text = "".join(b.text for b in blocks[last + 1 :] if b.type == "text")
    try:
        return _Output.model_validate_json(text)
    except ValidationError as e:
        raise ValueError(f"output doesn't match the schema: {e.errors()[0]['msg']}") from None


class TzNamer:
    def __init__(self, client: Any, budget: Budget, config: NamerConfig | None = None) -> None:
        self.client = client
        self.budget = budget
        self.config = config or NamerConfig()
        self.request = build_request(self.config)

    def __call__(self, sg: Suggestion, ctx: ToolContext) -> Naming:
        problem = ""
        messages = None
        shown: dict[str, Message] = {}
        for _ in range(MAX_ATTEMPTS):
            if not self.budget.can_afford_critic():
                raise NameUnavailable("not enough budget left for a naming call")
            if messages is None:  # rendered after the budget check: rendering logs egress
                text, shown = render(sg, ctx)
                messages = [{"role": "user", "content": text}]
            try:
                response = send(self.client, self.request, messages)
            except anthropic.APIError as e:
                raise NameUnavailable(f"API error ({type(e).__name__})") from None
            self.budget.add(response, kind="critic")
            if response.stop_reason == "refusal":
                raise NameUnavailable("the model declined")
            try:
                out = _parse(response)
            except ValueError as e:
                problem = str(e)
                continue
            last = ctx.messages[-1].sent_at_utc if ctx.messages else None
            return check(out, sg, shown, last)
        raise NameUnavailable(f"malformed output after {MAX_ATTEMPTS} tries: {problem}")
