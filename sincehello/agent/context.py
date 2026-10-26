"""State the agent's tools share during one run: the person's messages, the egress log,
and every query result the agent has seen (so `propose_insight` can recompute it).

Everything here runs locally over the store. Nothing in this module talks to Claude.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sincehello import config
from sincehello.analysis import recompute as recompute_stat
from sincehello.analysis.tz import TzResolver
from sincehello.report.schema import Insight
from sincehello.store import Store
from sincehello.store.store import ME_PERSON_ID

MAX_TEXT = 500  # characters of one message sent to Claude (SPEC.md §7.4)

# The person's 1:1 messages, reactions excluded, from me or from them. Same scope as the
# stats engine (analysis/dataset.py): analysis covers 1:1 conversations only.
_SCOPE_SQL = """
    SELECT m.id, m.source, m.source_msg_id, m.conversation_id, m.is_from_me,
           m.sent_at_utc, m.text
      FROM message m
      LEFT JOIN handle h ON h.id = m.sender_handle_id
     WHERE m.is_reaction = 0
       AND m.conversation_id IN (
           SELECT c.id FROM conversation c
             JOIN conversation_member cm ON cm.conversation_id = c.id
             JOIN handle hm ON hm.id = cm.handle_id
            WHERE c.is_group = 0 AND hm.person_id = ?)
       AND (m.is_from_me = 1 OR h.person_id = ?)
     ORDER BY m.sent_at_utc, m.id
"""


class ToolError(Exception):
    """A failure the model should see as an `is_error` tool result, with this message."""


@dataclass(frozen=True, slots=True)
class Message:
    rowid: int
    id: str  # "source:source_msg_id", the id evidence cites (SPEC.md §8.1)
    conversation_id: int
    sender: str  # "me" or "them"
    sent_at_utc: int  # ms
    text: str


def iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def parse_when(value: str | None) -> int | None:
    """A UTC date ("2021-05-14") or datetime ("2021-05-14T03:00:00Z") to ms."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"bad date {value!r}; use YYYY-MM-DD or an ISO datetime (UTC)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Patterns match like the eval manifest defines: case-insensitive `re.search`."""
    if len(pattern) > 200:
        raise ToolError("pattern is too long (max 200 characters)")
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"invalid regex {pattern!r}: {e}") from None


def query_hash(kind: str, args: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
    return f"q_{kind}_{digest[:10]}"


@dataclass
class EgressLog:
    """Every message id returned to Claude, in order (SPEC.md §7.4, §12).

    `with_text` is False when only the id went out (e.g. inside a stat result).
    """

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, turn: int, tool: str, message_id: str, with_text: bool) -> None:
        self.entries.append(
            {"turn": turn, "tool": tool, "message_id": message_id, "with_text": with_text}
        )

    def shared_ids(self) -> set[str]:
        return {e["message_id"] for e in self.entries if e["with_text"]}

    def summary(self, total_messages: int) -> dict[str, Any]:
        shared = self.shared_ids()
        referenced = {e["message_id"] for e in self.entries}
        return {
            "messages_shared": len(shared),
            "ids_referenced": len(referenced),
            "total_messages": total_messages,
            "pct_shared": round(100 * len(shared) / total_messages, 2) if total_messages else 0.0,
        }


class ToolContext:
    """One person's messages plus the bookkeeping the tools need."""

    def __init__(self, store: Store, person_id: int, default_tz: str | None = None) -> None:
        person = store.conn.execute(
            "SELECT id, display_name, is_me FROM person WHERE id = ?", (person_id,)
        ).fetchone()
        if person is None or person["is_me"]:
            raise ValueError(f"unknown person {person_id}")
        self.store = store
        self.person_id = person_id
        self.person_name: str | None = person["display_name"]
        self.default_tz = default_tz or config.default_timezone()
        self.tz = TzResolver(store, self.default_tz)
        self.egress = EgressLog()
        self.turn = 0  # set by the loop before each batch of tool calls
        # query_id -> {"kind", "args", "value"} for every result the model has seen.
        self.queries: dict[str, dict[str, Any]] = {}
        # Filled by propose_insight / finish.
        self.insights: list[Insight] = []
        self.rejected_proposals = 0
        self.finished = False
        self.summary: str | None = None
        # M5 hook: a `tools.Critic` run on insights that pass the deterministic checks.
        self.critic: Any = None
        self.eras: Any = ...  # detect_eras() result, loaded on first use (tools._eras)

        self.messages: list[Message] = [
            Message(
                r["id"],
                f"{r['source']}:{r['source_msg_id']}",
                r["conversation_id"],
                "me" if r["is_from_me"] else "them",
                r["sent_at_utc"],
                r["text"] or "",
            )
            for r in store.conn.execute(_SCOPE_SQL, (person_id, person_id))
        ]
        self.by_id: dict[str, int] = {m.id: i for i, m in enumerate(self.messages)}
        self._times = [m.sent_at_utc for m in self.messages]

    # --- lookups ----------------------------------------------------------------

    def get(self, message_id: str) -> Message:
        """A message in scope, or a ToolError explaining why it isn't."""
        i = self.by_id.get(message_id)
        if i is not None:
            return self.messages[i]
        source, _, source_msg_id = message_id.partition(":")
        row = self.store.conn.execute(
            "SELECT m.is_reaction, c.is_group FROM message m"
            " JOIN conversation c ON c.id = m.conversation_id"
            " WHERE m.source = ? AND m.source_msg_id = ?",
            (source, source_msg_id),
        ).fetchone()
        if row is None:
            raise ToolError(f"no message with id {message_id!r} (ids look like 'source:xyz')")
        if row["is_reaction"]:
            raise ToolError(f"{message_id} is a reaction (tapback), not a message")
        if row["is_group"]:
            raise ToolError(f"{message_id} is in a group chat; only 1:1 messages count")
        raise ToolError(f"{message_id} is not in your 1:1 conversation with this person")

    def window(self, start_utc: int | None, end_utc: int | None) -> list[Message]:
        lo = 0 if start_utc is None else bisect_left(self._times, start_utc)
        hi = len(self.messages) if end_utc is None else bisect_left(self._times, end_utc)
        return self.messages[lo:hi]

    def local_time(self, m: Message) -> datetime:
        person = ME_PERSON_ID if m.sender == "me" else self.person_id
        return self.tz.local(person, m.sent_at_utc)

    # --- what goes to Claude --------------------------------------------------

    def show(self, m: Message, tool: str) -> dict[str, Any]:
        """One message as the model sees it. The only place message text leaves the store."""
        self.egress.record(self.turn, tool, m.id, with_text=True)
        text = m.text if len(m.text) <= MAX_TEXT else m.text[:MAX_TEXT] + "…[truncated]"
        return {
            "id": m.id,
            "sender": m.sender,
            "sent_at_utc": iso(m.sent_at_utc),
            "local_time": self.local_time(m).isoformat(timespec="minutes"),
            "text": text,
        }

    def note_ids(self, obj: Any, tool: str) -> None:
        """Log message ids that appear inside a result without their text."""
        if isinstance(obj, str):
            if obj in self.by_id:
                self.egress.record(self.turn, tool, obj, with_text=False)
        elif isinstance(obj, dict):
            for v in obj.values():
                self.note_ids(v, tool)
        elif isinstance(obj, list | tuple):
            for v in obj:
                self.note_ids(v, tool)

    # --- deterministic queries the agent can cite as metrics ------------------

    def first_match(self, pattern: str, sender: str = "any") -> dict[str, Any]:
        rx = compile_pattern(pattern)
        hits = [m for m in self.messages if _sender_ok(m, sender) and rx.search(m.text)]
        first = hits[0] if hits else None
        return {
            "message_id": first.id if first else None,
            "sender": first.sender if first else None,
            "sent_at_utc": iso(first.sent_at_utc) if first else None,
            "matches": len(hits),
        }

    def count_matches(self, pattern: str, sender: str = "any", group_by: str = "none") -> dict:
        rx = compile_pattern(pattern)
        hits = [m for m in self.messages if _sender_ok(m, sender) and rx.search(m.text)]
        groups: dict[str, int] = {}
        for m in hits:
            if group_by == "sender":
                key = m.sender
            elif group_by == "year":
                key = f"{self.local_time(m):%Y}"
            elif group_by == "month":
                key = f"{self.local_time(m):%Y-%m}"
            else:
                continue
            groups[key] = groups.get(key, 0) + 1
        out: dict[str, Any] = {"total": len(hits)}
        if group_by != "none":
            out["groups"] = dict(sorted(groups.items()))
        return out

    def register(self, query_id: str, kind: str, args: dict[str, Any], value: Any) -> None:
        self.queries[query_id] = {"kind": kind, "args": args, "value": value}

    def recompute(self, query_id: str) -> Any:
        """Re-run a query the model saw this run, from its recorded arguments."""
        q = self.queries.get(query_id)
        if q is None:
            raise ToolError(
                f"unknown query_id {query_id!r}; cite one returned by get_stats, count or"
                " first_occurrence in this run"
            )
        if q["kind"] == "stat":
            return recompute_stat(self.store, q["args"]).value
        if q["kind"] == "first_occurrence":
            return self.first_match(**q["args"])
        if q["kind"] == "count":
            return self.count_matches(**q["args"])
        raise ToolError(f"query {query_id} can't be recomputed")


def _sender_ok(m: Message, sender: str) -> bool:
    return sender in ("any", m.sender)
