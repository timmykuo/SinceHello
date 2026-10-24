"""Optional Claude rewrite pass: richer filler text, same planted facts (SPEC.md §10.1).

The offline generator decides everything that matters for ground truth: timestamps,
senders, conversations, flags, and every planted message. This pass only rewrites the
*text* of filler messages, era by era, in chunks. Planted messages are shown to Claude
as locked context and never change. Each rewritten line is validated (single line,
length, no reserved terms) and falls back to the offline text when it fails, so the
manifest's facts are identical to the offline run. ``rewrite`` recomputes and
re-validates the manifest to prove it.

Needs the ``llm`` extra (``uv sync --extra llm``) and an ``ANTHROPIC_API_KEY`` (or an
``ant auth login`` profile). Cost scales with history size: roughly one request per
120 lines; use ``max_chunks`` to cap a trial run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from evals.generator import manifest as manifest_mod
from evals.generator.clock import Clock
from evals.generator.scenario import Persona, Scenario, parse_utc

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
CHUNK_LINES = 120
MAX_TEXT = 280
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You write filler text messages for a synthetic chat history. The history is test data \
for software that finds insights in a couple's texts, so it must read like real, mundane \
texting while leaving the planted test facts untouched.

You get one chunk of a conversation: each line has an id, the conversation (dm = the \
couple's 1:1 chat, group = a friends' group chat), the sender, the sender's local time, \
and the current text. Lines marked SLOT need new text. Lines marked LOCKED are fixed \
context; never rewrite them and never return their ids.

For every SLOT line, write one message that:
- comes from that sender and fits their texting style, the local time of day, the era \
summary, and the surrounding lines (reply to what was just said where it makes sense)
- is a single line of plain English, usually under 15 words, never over 200 characters
- does not mention any reserved term (listed in the request), not even in another form
- does not create new milestones: no first "i love you", no new pet names, no new inside \
jokes, no breakups, no moves, no trips that are not in the era summary
- keeps attachment-free, everyday content: plans, food, work, shows, small talk

Return every SLOT id exactly once."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}


@dataclass
class Chunk:
    era: dict[str, Any]
    rows: list[dict[str, Any]]

    @property
    def slot_ids(self) -> list[str]:
        return [r["id"] for r in self.rows if is_slot(r)]

    def handle(self, i: int) -> str:
        """Short per-chunk line handle used in prompts (ids can contain reserved words)."""
        return f"L{i + 1}"

    def slot_handles(self) -> dict[str, str]:
        return {self.handle(i): r["id"] for i, r in enumerate(self.rows) if is_slot(r)}


@dataclass
class RewriteReport:
    model: str
    chunks: int = 0
    requests: int = 0
    rewritten: int = 0
    rejected: int = 0
    refusals: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    notes: list[str] = field(default_factory=list)


def is_slot(r: dict[str, Any]) -> bool:
    return r["fact"] is None and not r["is_reaction"] and r["text"] is not None


def chunks(
    records: list[dict[str, Any]], manifest: dict[str, Any], size: int = CHUNK_LINES
) -> list[Chunk]:
    """Split the history era by era into chunks of at most ``size`` lines."""
    out = []
    for era in manifest["eras"]:
        lo, hi = parse_utc(era["start_utc"]), parse_utc(era["end_utc"])
        rows = [r for r in records if not r["is_reaction"] and lo <= parse_utc(r["ts"]) < hi]
        for i in range(0, len(rows), size):
            part = rows[i : i + size]
            if any(is_slot(r) for r in part):
                out.append(Chunk(era, part))
    return out


def _style(p: Persona) -> str:
    s = p.style
    bits = [
        f"lowercase {int(s.lowercase * 100)}% of the time",
        f"emoji in {int(s.emoji_rate * 100)}% of messages ({' '.join(s.emojis)})",
        "often double-texts" if s.double_text > 0.3 else "usually one message per turn",
    ]
    if s.abbrev > 0.25:
        bits.append("uses abbreviations like u, ur, tbh")
    return "; ".join(bits)


def era_summary(scenario: Scenario, era: dict[str, Any]) -> str:
    spec = next(e for e in scenario.eras if e.id == era["id"])
    return (
        f'"{era["name"]}" ({era["start_utc"][:10]} to {era["end_utc"][:10]}); '
        f"usual topics: {', '.join(dict.fromkeys(spec.topics))}"
    )


def build_prompt(scenario: Scenario, manifest: dict[str, Any], chunk: Chunk) -> str:
    clock = Clock(scenario)
    names = {k: v["name"] for k, v in manifest["participants"].items()}
    lines = []
    for i, r in enumerate(chunk.rows):
        local = clock.local(r["sender"], parse_utc(r["ts"]))
        mark = "SLOT" if is_slot(r) else "LOCKED"
        body = r["text"] if r["text"] is not None else "[photo]"
        lines.append(
            f"{chunk.handle(i)} | {r['conv']} | {names[r['sender']]} | "
            f"{local:%a %Y-%m-%d %H:%M} | {mark} | {json.dumps(body, ensure_ascii=False)}"
        )
    reserved = ", ".join(p.pattern for p in scenario.reserved_patterns())
    return "\n".join(
        [
            f"Couple: {scenario.me.name} and {scenario.them.name}.",
            f"{scenario.me.name}'s style: {_style(scenario.me)}.",
            f"{scenario.them.name}'s style: {_style(scenario.them)}.",
            f"Era: {era_summary(scenario, chunk.era)}.",
            f"Reserved terms (regular expressions, case-insensitive): {reserved}",
            "",
            "Lines (id | conversation | sender | sender local time | SLOT/LOCKED | text):",
            *lines,
        ]
    )


def parse_response(
    raw: str, chunk: Chunk, reserved: list[re.Pattern[str]], report: RewriteReport | None = None
) -> dict[str, str]:
    """Return {message id: new text} for the lines that pass validation; drop the rest."""
    try:
        items = json.loads(raw)["lines"]
    except (json.JSONDecodeError, KeyError, TypeError):
        if report:
            report.notes.append("unparseable response; kept offline text for the chunk")
            report.rejected += len(chunk.slot_ids)
        return {}
    wanted = chunk.slot_handles()
    out: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        handle, text = item.get("id"), item.get("text")
        if handle not in wanted or handle in out or not isinstance(text, str):
            continue
        text = " ".join(text.split())
        if not text or len(text) > MAX_TEXT or any(p.search(text) for p in reserved):
            continue
        out[handle] = text
    out = {wanted[h]: t for h, t in out.items()}
    if report:
        report.rejected += len(wanted) - len(out)
    return out


def request(client: Any, model: str, effort: str, prompt: str) -> Any:
    return client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )


def rewrite(
    scenario: Scenario,
    records: list[dict[str, Any]],
    manifest: dict[str, Any],
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_chunks: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], RewriteReport]:
    """Rewrite filler text with Claude. Returns new records, manifest, and a report."""
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    reserved = scenario.reserved_patterns()
    records = [dict(r) for r in records]
    by_id = {r["id"]: r for r in records}
    report = RewriteReport(model=model)
    todo = chunks(records, manifest)
    for chunk in todo[:max_chunks]:
        report.chunks += 1
        resp = request(client, model, effort, build_prompt(scenario, manifest, chunk))
        report.requests += 1
        usage = getattr(resp, "usage", None)
        report.input_tokens += getattr(usage, "input_tokens", 0) or 0
        report.output_tokens += getattr(usage, "output_tokens", 0) or 0
        if resp.stop_reason == "refusal":
            report.refusals += 1
            report.rejected += len(chunk.slot_ids)
            report.notes.append(f"refusal in era {chunk.era['id']}; kept offline text")
            continue
        if resp.stop_reason == "max_tokens":
            report.rejected += len(chunk.slot_ids)
            report.notes.append(f"truncated response in era {chunk.era['id']}; kept offline text")
            continue
        raw = next((b.text for b in resp.content if b.type == "text"), "")
        for mid, text in parse_response(raw, chunk, reserved, report).items():
            by_id[mid]["text"] = text
            report.rewritten += 1
    _requote_reactions(records, by_id)
    meta = {
        **manifest["generator"],
        "kind": "llm",
        "base": "offline",
        "model": model,
        "effort": effort,
        "rewritten": report.rewritten,
        "rejected": report.rejected,
        "chunks": report.chunks,
        "chunks_total": len(todo),
    }
    new_manifest = manifest_mod.compute(scenario, records, manifest["seed"], meta)
    manifest_mod.validate(scenario, records, new_manifest)
    if new_manifest["facts"] != manifest["facts"]:
        raise ValueError("LLM rewrite changed planted facts; this is a bug in validation")
    return records, new_manifest, report


def _requote_reactions(records: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> None:
    """Reactions quote their target's text; keep the quote in sync after a rewrite."""
    for r in records:
        if r["is_reaction"] and r["reacts_to"] and r["fact"] is None:
            target = by_id[r["reacts_to"]]
            verb = r["text"].split(" “", 1)[0]
            if target["text"] is not None:
                r["text"] = f"{verb} “{target['text']}”"
