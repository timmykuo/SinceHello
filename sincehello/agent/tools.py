"""The discovery agent's tools (SPEC.md §7.4): the only window Claude has into the messages.

Each tool is a plain function `tool(ctx, **input) -> dict` over the store. `build_tools`
pairs each one with its JSON schema (`strict: true`), and `dispatch` runs a `tool_use`
block and turns the outcome into a `tool_result`.

Caps: at most 25 messages per search, 15 context messages each side, 40 per era sample,
and texts truncated to ~500 characters (`ToolContext.show`, which also writes the egress
log). A tool raises `ToolError` for anything the model should fix; that becomes an
`is_error` result carrying the reason.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from sincehello.agent.context import (
    MAX_TEXT,
    ToolContext,
    ToolError,
    compile_pattern,
    iso,
    parse_when,
    query_hash,
)
from sincehello.analysis import STATS, compute, compute_all
from sincehello.report.schema import Insight, InsightDraft, Scores
from sincehello.verify import checks

MAX_SEARCH = 25
MAX_CONTEXT = 15
MAX_SAMPLE = 40
MAX_CANDIDATES = 20

ERAS_MODULE = "sincehello.analysis.eras"  # detect_eras, sample_era (branch m2b-eras-miners)
MINERS_MODULE = "sincehello.analysis.miners"  # mine


# --- M5 hook: the LLM critic --------------------------------------------------------


@dataclass
class Verdict:
    verdict: Literal["pass", "revise", "reject"]
    issues: list[str]
    scores: Scores | None = None


class Critic(Protocol):
    """SPEC.md §7.6 step 2. Runs on insights that passed the deterministic checks.

    Not implemented in M4: with no critic, accepted insights carry critic="pending".
    """

    def __call__(self, insight: Insight, ctx: ToolContext) -> Verdict: ...


# --- tools --------------------------------------------------------------------------


def get_overview(ctx: ToolContext) -> dict[str, Any]:
    names = [
        "totals",
        "first_message",
        "who_texts_first",
        "reply_times",
        "busiest_day",
        "longest_silence",
        "longest_streak",
        "late_night",
    ]
    results = compute_all(ctx.store, ctx.person_id, names, default_tz=ctx.default_tz)
    stats = {}
    for name, r in results.items():
        ctx.register(r.query_id, "stat", r.to_dict(), r.value)
        value = r.value
        if name == "totals":  # by_month is long; get_stats("totals") has it
            value = {k: v for k, v in value.items() if k != "by_month"}
        stats[name] = {"query_id": r.query_id, "value": value}
    ctx.note_ids(stats, "get_overview")
    me = ctx.store.conn.execute("SELECT display_name FROM person WHERE id = 1").fetchone()
    sources = sorted({m.id.split(":", 1)[0] for m in ctx.messages})
    out: dict[str, Any] = {
        "person": {"id": ctx.person_id, "name": ctx.person_name},
        "me": {"name": me["display_name"] if me else None},
        "sources": sources,
        "range": {
            "first_utc": iso(ctx.messages[0].sent_at_utc) if ctx.messages else None,
            "last_utc": iso(ctx.messages[-1].sent_at_utc) if ctx.messages else None,
        },
        "default_tz": ctx.default_tz,
        "stats": stats,
        "available_stats": {s.name: s.summary for s in STATS.values()},
    }
    eras = _eras(ctx)
    out["eras"] = [e.to_dict() for e in eras] if eras is not None else "not available yet"
    return out


def get_stats(ctx: ToolContext, name: str, params: dict[str, Any] | None = None) -> dict:
    if name not in STATS:
        raise ToolError(f"unknown stat {name!r}; known: {', '.join(STATS)}")
    params = dict(params or {})
    for key in ("start", "end"):
        if key in params:
            params[f"{key}_utc"] = parse_when(params.pop(key))
    if STATS[name].local_time:
        params.setdefault("default_tz", ctx.default_tz)
    try:
        result = compute(ctx.store, ctx.person_id, name, params)
    except ValueError as e:
        raise ToolError(str(e)) from None
    d = result.to_dict()
    ctx.register(result.query_id, "stat", d, result.value)
    ctx.note_ids(d["value"], "get_stats")
    return {k: v for k, v in d.items() if k != "person_id"}


def search_messages(
    ctx: ToolContext,
    query: str,
    mode: str = "keyword",
    sender: str = "any",
    start: str | None = None,
    end: str | None = None,
    order: str = "oldest",
    limit: int = 10,
) -> dict[str, Any]:
    pool = ctx.window(parse_when(start), parse_when(end))
    if sender != "any":
        pool = [m for m in pool if m.sender == sender]
    if mode == "regex":
        rx = compile_pattern(query)
        hits = [m for m in pool if rx.search(m.text)]
    elif mode == "keyword":
        try:
            rowids = {
                r[0]
                for r in ctx.store.conn.execute(
                    "SELECT rowid FROM message_fts WHERE message_fts MATCH ?", (query,)
                )
            }
        except sqlite3.OperationalError as e:
            raise ToolError(
                f"FTS5 query error: {e}. Put phrases in double quotes, or use mode='regex'."
            ) from None
        hits = [m for m in pool if m.rowid in rowids]
    else:
        raise ToolError("mode must be 'keyword' or 'regex'")
    if order == "newest":
        hits = hits[::-1]
    limit = max(1, min(limit, MAX_SEARCH))
    return {
        "total_matches": len(hits),
        "returned": min(limit, len(hits)),
        "messages": [ctx.show(m, "search_messages") for m in hits[:limit]],
    }


def first_occurrence(ctx: ToolContext, pattern: str, sender: str = "any") -> dict[str, Any]:
    args = {"pattern": pattern, "sender": sender}
    value = ctx.first_match(**args)
    qid = query_hash("first", args)
    ctx.register(qid, "first_occurrence", args, value)
    out: dict[str, Any] = {"query_id": qid, **value}
    if value["message_id"]:
        out["message"] = ctx.show(ctx.get(value["message_id"]), "first_occurrence")
    return out


def count(
    ctx: ToolContext, pattern: str, sender: str = "any", group_by: str = "none"
) -> dict[str, Any]:
    args = {"pattern": pattern, "sender": sender, "group_by": group_by}
    value = ctx.count_matches(**args)
    qid = query_hash("count", args)
    ctx.register(qid, "count", args, value)
    return {"query_id": qid, **value}


def get_context(
    ctx: ToolContext, message_id: str, before: int = 5, after: int = 5
) -> dict[str, Any]:
    target = ctx.get(message_id)
    convo = [m for m in ctx.messages if m.conversation_id == target.conversation_id]
    i = convo.index(target)
    before, after = max(0, min(before, MAX_CONTEXT)), max(0, min(after, MAX_CONTEXT))
    window = convo[max(0, i - before) : i + after + 1]
    return {
        "target": message_id,
        "messages": [ctx.show(m, "get_context") for m in window],
    }


def sample_era(
    ctx: ToolContext, era_id: str, n: int = 20, strategy: str = "random", seed: int = 0
) -> dict[str, Any]:
    eras_mod = _optional_module(ERAS_MODULE)
    eras = _eras(ctx) or []
    era = next((e for e in eras if e.id == era_id), None)
    if eras_mod is None or era is None:
        raise ToolError(f"unknown era {era_id!r}; known: {[e.id for e in eras]}")
    rows = eras_mod.sample_era(
        ctx.store, ctx.person_id, era, n=max(1, min(n, MAX_SAMPLE)), strategy=strategy, seed=seed
    )
    shown = []
    for r in rows:
        mid = f"{r['source']}:{r['source_msg_id']}"
        if mid in ctx.by_id:  # never show anything outside the 1:1 scope
            shown.append(ctx.show(ctx.get(mid), "sample_era"))
    return {"era": era.to_dict(), "strategy": strategy, "messages": shown}


def list_candidates(ctx: ToolContext, kind: str = "") -> dict[str, Any]:
    miners = _optional_module(MINERS_MODULE)
    if miners is None:
        raise ToolError("candidate miners are not available yet")
    found = miners.mine(ctx.store, ctx.person_id, kind or None, limit=MAX_CANDIDATES)
    out = [_truncate(c.to_dict()) for c in found[:MAX_CANDIDATES]]
    # Candidates may carry snippets, so count their ids as shared with text.
    for mid in _ids_in(out, ctx):
        ctx.egress.record(ctx.turn, "list_candidates", mid, with_text=True)
    return {"kind": kind or "all", "candidates": out}


def propose_insight(ctx: ToolContext, **insight: Any) -> dict[str, Any]:
    try:
        draft = InsightDraft.model_validate(insight)
    except ValidationError as e:
        ctx.rejected_proposals += 1
        reasons = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or 'insight'}: {err['msg']}" for err in e.errors()
        )
        raise ToolError(f"invalid insight: {reasons}") from None
    eras = _eras(ctx)
    failures = checks.check(ctx, draft, {e.id for e in eras} if eras is not None else None)
    if failures:
        ctx.rejected_proposals += 1
        raise ToolError("insight failed verification:\n- " + "\n- ".join(failures))
    ins = Insight(id=f"ins_{len(ctx.insights) + 1:03d}", **draft.model_dump())
    ins.verification.deterministic = "pass"
    if ctx.critic is not None:  # M5: LLM critic + revise loop (SPEC.md §7.6 step 2)
        verdict = ctx.critic(ins, ctx)
        if verdict.verdict != "pass":
            ctx.rejected_proposals += 1
            raise ToolError(f"critic says {verdict.verdict}:\n- " + "\n- ".join(verdict.issues))
        ins.verification.critic = "pass"
        ins.scores = verdict.scores or ins.scores
    ctx.insights.append(ins)
    return {"status": "accepted", "id": ins.id, "insights_so_far": len(ctx.insights)}


def finish(ctx: ToolContext, summary: str) -> dict[str, Any]:
    ctx.finished = True
    ctx.summary = summary
    return {"status": "finished", "insights": len(ctx.insights)}


# --- schemas ------------------------------------------------------------------------

SENDER = {"type": "string", "enum": ["me", "them", "any"]}
DATE = {"type": "string", "description": "UTC date YYYY-MM-DD or ISO datetime"}


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _stat_params_schema() -> dict[str, Any]:
    """Every parameter any stat takes, typed from its default. Wrong ones get an error."""
    props: dict[str, Any] = {"start": DATE, "end": DATE}
    for spec in STATS.values():
        for p, default in spec.defaults.items():
            kind = "number" if isinstance(default, float) else "integer"
            props.setdefault(p, {"type": kind})
    return _obj(dict(sorted(props.items())), [])


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: Callable[..., dict[str, Any]]

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


def _insight_schema() -> dict[str, Any]:
    evidence = _obj(
        {
            "message_id": {"type": "string"},
            "quote": {"type": "string", "description": "Copied verbatim from the message."},
        },
        ["message_id", "quote"],
    )
    metric = _obj(
        {
            "query_id": {"type": "string"},
            "path": {
                "type": "string",
                "description": "Dotted path into the query's value, e.g. 'by_sender.them'"
                " or 'top.0.date'; '' for the whole value.",
            },
            "value": {
                "type": "string",
                "description": 'The claimed value at that path, as JSON (412, 0.63, "them").',
            },
        },
        ["query_id", "path", "value"],
    )
    first = _obj(
        {"pattern": {"type": "string"}, "sender": SENDER},
        ["pattern", "sender"],
    )
    return _obj(
        {
            "category": {
                "type": "string",
                "enum": ["milestone", "language", "rhythm", "story", "free"],
            },
            "kind": {
                "type": "string",
                "description": "first_occurrence, count, stat, pet_name, inside_joke, era,"
                " moment, or another short snake_case kind.",
            },
            "title": {"type": "string"},
            "headline": {"type": "string"},
            "body": {"type": "string"},
            "evidence": {"type": "array", "items": evidence},
            "metric": metric,
            "era_id": {"type": "string"},
            "first": first,
        },
        ["category", "kind", "title", "headline", "body", "evidence"],
    )


def build_tools() -> list[Tool]:
    """The tool list, in a fixed order so the cached prompt prefix never changes."""
    tools = [
        Tool(
            "get_overview",
            "Orientation: names, date range, totals, eras, and headline stats with query_ids."
            " Call this first.",
            _obj({}, []),
            get_overview,
        ),
        Tool(
            "get_stats",
            "One stat block from the local stats engine (numbers only, no message text)."
            " Returns a query_id to cite in an insight's metric. Optional params: start/end"
            " window plus the stat's own parameters (see get_overview.available_stats).",
            _obj(
                {
                    "name": {"type": "string", "enum": sorted(STATS)},
                    "params": _stat_params_schema(),
                },
                ["name"],
            ),
            get_stats,
        ),
        Tool(
            "search_messages",
            f"Find messages. mode='keyword' uses SQLite FTS5 syntax (quote phrases:"
            f" \"goose incident\"); mode='regex' is a case-insensitive Python regex. Returns"
            f" at most {MAX_SEARCH} messages (id, sender, UTC and sender-local time, text"
            f" truncated to {MAX_TEXT} chars) plus the total match count.",
            _obj(
                {
                    "query": {"type": "string"},
                    "mode": {"type": "string", "enum": ["keyword", "regex"]},
                    "sender": SENDER,
                    "start": DATE,
                    "end": DATE,
                    "order": {"type": "string", "enum": ["oldest", "newest"]},
                    "limit": {"type": "integer", "description": f"1-{MAX_SEARCH}, default 10"},
                },
                ["query", "mode"],
            ),
            search_messages,
        ),
        Tool(
            "first_occurrence",
            "The earliest message matching a case-insensitive regex, optionally from one"
            " sender. The deterministic answer to 'the first time...'. Returns a query_id.",
            _obj({"pattern": {"type": "string"}, "sender": SENDER}, ["pattern"]),
            first_occurrence,
        ),
        Tool(
            "count",
            "How many messages match a case-insensitive regex, optionally grouped by sender,"
            " year or month (sender-local). Returns a query_id for 'you said X N times'.",
            _obj(
                {
                    "pattern": {"type": "string"},
                    "sender": SENDER,
                    "group_by": {"type": "string", "enum": ["none", "sender", "year", "month"]},
                },
                ["pattern"],
            ),
            count,
        ),
        Tool(
            "get_context",
            f"Messages around one message in the same conversation (up to {MAX_CONTEXT}"
            " before and after, default 5). Use it to read a moment before claiming it.",
            _obj(
                {
                    "message_id": {"type": "string"},
                    "before": {"type": "integer"},
                    "after": {"type": "integer"},
                },
                ["message_id"],
            ),
            get_context,
        ),
    ]
    if _optional_module(ERAS_MODULE) is not None:
        tools.append(
            Tool(
                "sample_era",
                f"A sample of up to {MAX_SAMPLE} messages from one era (ids from"
                " get_overview.eras), to name and summarize it.",
                _obj(
                    {
                        "era_id": {"type": "string"},
                        "n": {"type": "integer"},
                        "strategy": {
                            "type": "string",
                            "enum": ["random", "spike_days", "longest_threads"],
                        },
                        "seed": {"type": "integer"},
                    },
                    ["era_id"],
                ),
                sample_era,
            )
        )
    if _optional_module(MINERS_MODULE) is not None:
        tools.append(
            Tool(
                "list_candidates",
                "Leads from the local candidate miners (firsts, pet names, spikes, conflicts)."
                " Leads, not facts: verify before proposing. Empty kind lists all.",
                _obj({"kind": {"type": "string"}}, []),
                list_candidates,
            )
        )
    tools += [
        Tool(
            "propose_insight",
            "Submit one insight. It is checked immediately: cited ids must exist in your 1:1"
            " chat, quotes must be verbatim, 'first' claims are re-run, and metrics must"
            " recompute to the claimed value. On failure you get the reasons; fix and resubmit.",
            _insight_schema(),
            propose_insight,
        ),
        Tool(
            "finish",
            "End the discovery run with a short summary of what you found.",
            _obj({"summary": {"type": "string"}}, ["summary"]),
            finish,
        ),
    ]
    return tools


# --- dispatch -----------------------------------------------------------------------


@dataclass
class Outcome:
    content: str
    is_error: bool
    internal_error: str | None = None  # traceback, for the run log only


def dispatch(tools: dict[str, Tool], ctx: ToolContext, name: str, args: dict) -> Outcome:
    tool = tools.get(name)
    if tool is None:
        return Outcome(f"unknown tool {name!r}", True)
    try:
        out = tool.func(ctx, **args)
    except ToolError as e:
        return Outcome(str(e), True)
    except TypeError as e:  # strict schemas make this rare
        return Outcome(f"bad arguments for {name}: {e}", True)
    except Exception as e:  # a bug in our code: tell the model, keep the run going
        return Outcome(
            f"internal error in {name}: {type(e).__name__}", True, traceback.format_exc()
        )
    return Outcome(json.dumps(out, ensure_ascii=False, separators=(",", ":")), False)


# --- helpers ------------------------------------------------------------------------


def _optional_module(name: str) -> ModuleType | None:
    """Import a module only if it exists (era/miner modules land from another branch)."""
    try:
        if importlib.util.find_spec(name) is None:
            return None
    except (ImportError, ValueError):
        return None
    return importlib.import_module(name)


def _eras(ctx: ToolContext) -> list[Any] | None:
    """detect_eras once per run; None when the eras module isn't there."""
    if ctx.eras is ...:
        mod = _optional_module(ERAS_MODULE)
        ctx.eras = mod.detect_eras(ctx.store, ctx.person_id) if mod else None
    return ctx.eras


def _truncate(obj: Any) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_TEXT else obj[:MAX_TEXT] + "…[truncated]"
    if isinstance(obj, dict):
        return {k: _truncate(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_truncate(v) for v in obj]
    return obj


def _ids_in(obj: Any, ctx: ToolContext) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str) and obj in ctx.by_id:
        found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found += _ids_in(v, ctx)
    elif isinstance(obj, list):
        for v in obj:
            found += _ids_in(v, ctx)
    return found
