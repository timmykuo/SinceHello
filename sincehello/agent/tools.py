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
    Draft,
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

MAX_REVISIONS = 2  # critic revise rounds per insight; then a revise becomes a reject


@dataclass
class Verdict:
    verdict: Literal["pass", "revise", "reject"]
    issues: list[str]
    scores: Scores | None = None


class CriticUnavailable(Exception):
    """The critic couldn't give a verdict: a refusal, malformed output twice, an API
    error, or no budget left. The insight is kept with critic="pending" (unverified)."""


class Critic(Protocol):
    """SPEC.md §7.6 step 2. Runs on insights that passed the deterministic checks.

    On a resubmission, `insight.verification.revisions` is the round (1 or 2) and
    `insight.verification.critic_notes` holds the previous round's issues. Raises
    `CriticUnavailable` when it can't judge. With no critic (`--no-critic`), accepted
    insights carry critic="pending". The real one is `verify.critic.LLMCritic`.
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
        # Each stat's own parameters with defaults, so get_stats.params aren't guessed.
        "available_stats": {
            s.name: {"summary": s.summary, "params": s.defaults} for s in STATS.values()
        },
    }
    eras = _eras(ctx)
    out["eras"] = [e.to_dict() for e in eras] if eras is not None else "not available yet"
    return out


def get_stats(
    ctx: ToolContext,
    name: str,
    start: str | None = None,
    end: str | None = None,
    params: list[dict[str, Any]] | None = None,
) -> dict:
    if name not in STATS:
        raise ToolError(f"unknown stat {name!r}; known: {', '.join(STATS)}")
    # A list of {name, value} pairs, not an object with one optional key per stat
    # parameter: strict schemas allow only 24 optional parameters across all tools.
    defaults = STATS[name].defaults
    kwargs: dict[str, Any] = {}
    for p in params or []:
        key, value = p["name"], p["value"]
        # The schema types every value "number". Unknown names fall through to compute,
        # whose error lists what the stat does take.
        if key in defaults and not isinstance(defaults[key], float):
            if value != int(value):
                raise ToolError(f"{key} must be an integer, got {value}")
            value = int(value)
        kwargs[key] = value
    for key, when in (("start", start), ("end", end)):
        if when is not None:
            kwargs[f"{key}_utc"] = parse_when(when)
    if STATS[name].local_time:
        kwargs.setdefault("default_tz", ctx.default_tz)
    try:
        result = compute(ctx.store, ctx.person_id, name, kwargs)
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
    if not query:  # browse: every message in the window, in time order
        hits = pool
    elif mode == "regex":
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
    before, after = max(0, min(before, MAX_CONTEXT)), max(0, min(after, MAX_CONTEXT))
    window = ctx.around(message_id, before, after)
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
    found = found[:MAX_CANDIDATES]
    # Miners cite store row ids (message.id); the agent cites "source:source_msg_id".
    store_ids = {i for c in found for i in c.message_ids}
    for c in found:
        store_ids |= set(_store_ids_in(c.details))
    to_agent = _agent_ids(ctx, store_ids)
    out = [_truncate(_map_ids(c.to_dict(), to_agent)) for c in found]
    # Candidates may carry snippets, so count their ids as shared with text.
    for mid in _ids_in(out, ctx):
        ctx.egress.record(ctx.turn, "list_candidates", mid, with_text=True)
    return {"kind": kind or "all", "candidates": out}


def propose_insight(ctx: ToolContext, revises: str = "", **insight: Any) -> dict[str, Any]:
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
    if ctx.critic is None:
        ctx.insights.append(ins)
        return {"status": "accepted", "id": ins.id, "insights_so_far": len(ctx.insights)}
    return _criticize(ctx, ins, _draft_for(ctx, draft, revises))


def _draft_for(ctx: ToolContext, draft: InsightDraft, revises: str) -> Draft:
    """The revise-loop entry this proposal belongs to, with its revision count bumped
    when it is a resubmission."""
    cited = {ev.message_id for ev in draft.evidence}
    if revises:
        d = ctx.drafts.get(revises)
        if d is None:
            raise ToolError(
                f"unknown draft {revises!r}: `revises` takes the draft id from a critic"
                " 'revise' result; leave it empty for a new insight"
            )
    else:
        # A resubmission without `revises` (same kind, a cited message in common) still
        # counts as one, so the revision cap can't be sidestepped.
        d = next(
            (
                d
                for d in ctx.drafts.values()
                if d.status in ("open", "rejected") and d.kind == draft.kind and d.cited & cited
            ),
            None,
        )
        if d is None:
            d = Draft(f"draft_{len(ctx.drafts) + 1:03d}", draft.kind, cited)
            ctx.drafts[d.id] = d
            return d
    if d.status != "open":
        ctx.rejected_proposals += 1
        raise ToolError(
            f"this resubmits {d.id}, which is closed ({d.status}); critic notes:\n- "
            + "\n- ".join(d.notes or ["none"])
            + "\nDon't resubmit it; propose a different insight."
        )
    d.revisions += 1
    d.cited |= cited
    return d


def _criticize(ctx: ToolContext, ins: Insight, d: Draft) -> dict[str, Any]:
    """SPEC.md §7.6 step 2: pass is accepted; revise goes back to the agent with the
    critic's notes, at most MAX_REVISIONS times; reject closes the draft."""
    ins.verification.revisions = d.revisions
    ins.verification.critic_notes = list(d.notes)  # the previous round's, for the critic
    try:
        verdict = ctx.critic(ins, ctx)
    except CriticUnavailable as e:
        d.status = "unverified"
        ins.verification.critic_notes = [f"critic unavailable: {e}"]
        ctx.insights.append(ins)
        return {
            "status": "accepted_unverified",
            "id": ins.id,
            "insights_so_far": len(ctx.insights),
            "note": f"The critic couldn't review this insight ({e}). It is kept but"
            " unverified. Carry on with other insights.",
        }
    d.notes = list(verdict.issues)
    if verdict.verdict == "pass":
        d.status = "accepted"
        ins.verification.critic = "pass"
        ins.verification.critic_notes = list(verdict.issues)
        ins.scores = verdict.scores or ins.scores
        ctx.insights.append(ins)
        out: dict[str, Any] = {
            "status": "accepted",
            "id": ins.id,
            "insights_so_far": len(ctx.insights),
        }
        if verdict.issues:
            out["critic_notes"] = verdict.issues
        return out
    ctx.rejected_proposals += 1
    issues = "\n- ".join(verdict.issues or ["(no notes)"])
    if verdict.verdict == "revise" and d.revisions < MAX_REVISIONS:
        left = MAX_REVISIONS - d.revisions
        raise ToolError(
            f"critic says revise ({d.id}, {left} revision{'s' if left > 1 else ''} left):\n"
            f"- {issues}\nFix these and call propose_insight again with revises={d.id!r}."
            " If the evidence can't support the claim, drop it instead."
        )
    d.status = "rejected"
    why = (
        f"critic says {verdict.verdict} after {MAX_REVISIONS} revisions, so {d.id} is rejected"
        if verdict.verdict == "revise"
        else f"critic says reject ({d.id})"
    )
    raise ToolError(f"{why}:\n- {issues}\nThis insight is closed. Don't resubmit it; move on.")


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
    """Every parameter any stat takes, as {name, value} pairs. Wrong ones get an error."""
    names = sorted({p for spec in STATS.values() for p in spec.defaults})
    return {
        "type": "array",
        "items": _obj(
            {"name": {"type": "string", "enum": names}, "value": {"type": "number"}},
            ["name", "value"],
        ),
    }


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
    first = {
        **_obj({"pattern": {"type": "string"}, "sender": SENDER}, ["pattern", "sender"]),
        "description": "Only for kind first_occurrence (ignored otherwise): the pattern and"
        " sender you checked with first_occurrence.",
    }
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
            "revises": {
                "type": "string",
                "description": "Only when resubmitting an insight the critic sent back:"
                " the draft id from that result (e.g. 'draft_003').",
            },
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
            " Returns a query_id to cite in an insight's metric. Optional: a start/end"
            " window, and params as [{name, value}] pairs for the stat's own parameters"
            " (get_overview.available_stats lists each stat's parameters and defaults).",
            _obj(
                {
                    "name": {"type": "string", "enum": sorted(STATS)},
                    "start": DATE,
                    "end": DATE,
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
            f" truncated to {MAX_TEXT} chars) plus the total match count. An empty query"
            " matches every message: with start/end, it reads a time window in order"
            " (page through it with start or order='newest').",
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
            " recompute to the claimed value. On failure you get the reasons; fix and resubmit."
            f" Then an independent critic reads the cited messages in context: it may accept,"
            f" ask for a revision (resubmit with `revises`, at most {MAX_REVISIONS} times),"
            " or reject.",
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


def _agent_ids(ctx: ToolContext, store_ids: set[int]) -> dict[int, str]:
    """Map store message.id values to agent ids, keeping only messages in this run's scope."""
    if not store_ids:
        return {}
    marks = ",".join("?" * len(store_ids))
    rows = ctx.store.conn.execute(
        f"SELECT id, source, source_msg_id FROM message WHERE id IN ({marks})",
        sorted(store_ids),
    )
    out = {r["id"]: f"{r['source']}:{r['source_msg_id']}" for r in rows}
    return {k: v for k, v in out.items() if v in ctx.by_id}


def _is_id_key(key: str) -> bool:
    return key.endswith(("message_id", "message_ids"))


def _store_ids_in(obj: Any, key: str = "") -> list[int]:
    if isinstance(obj, dict):
        return [i for k, v in obj.items() for i in _store_ids_in(v, k)]
    if isinstance(obj, list | tuple):
        return [i for v in obj for i in _store_ids_in(v, key)]
    if isinstance(obj, int) and not isinstance(obj, bool) and _is_id_key(key):
        return [obj]
    return []


def _map_ids(obj: Any, to_agent: dict[int, str], key: str = "") -> Any:
    """Replace store ids under message-id keys with agent ids; drop ids outside the scope."""
    if isinstance(obj, dict):
        return {k: _map_ids(v, to_agent, k) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        mapped = [_map_ids(v, to_agent, key) for v in obj]
        return [v for v in mapped if v is not None] if _is_id_key(key) else mapped
    if isinstance(obj, int) and not isinstance(obj, bool) and _is_id_key(key):
        return to_agent.get(obj)
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
