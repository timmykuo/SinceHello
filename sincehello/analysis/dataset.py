"""Loads the messages one analysis covers: 1:1 conversations with a person, no reactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import cached_property

from sincehello.analysis.text import segments
from sincehello.analysis.tz import TzResolver
from sincehello.store import Store
from sincehello.store.store import ME_PERSON_ID

ME = "me"
THEM = "them"
SENDERS = (ME, THEM)

_ONE_TO_ONE_CONVERSATIONS = """
    SELECT c.id FROM conversation c
      JOIN conversation_member cm ON cm.conversation_id = c.id
      JOIN handle h ON h.id = cm.handle_id
     WHERE c.is_group = 0 AND h.person_id = ?
"""


@dataclass(frozen=True, slots=True)
class Msg:
    message_id: str  # "source:source_msg_id", the id evidence cites (SPEC.md §8.1)
    sender: str  # ME or THEM
    sent_at_utc: int  # ms
    text: str
    row_id: int  # message.id in the store


class Dataset:
    """Messages with one person in time order, plus each message's sender-local time."""

    def __init__(
        self,
        store: Store,
        person_id: int,
        default_tz: str,
        start_utc: int | None = None,
        end_utc: int | None = None,
    ) -> None:
        person = store.conn.execute(
            "SELECT id, display_name, is_me FROM person WHERE id = ?", (person_id,)
        ).fetchone()
        if person is None or person["is_me"]:
            raise ValueError(f"unknown person {person_id}")
        self.store = store
        self.person_id = person_id
        self.display_name: str | None = person["display_name"]
        self.default_tz = default_tz
        self.tz = TzResolver(store, default_tz)

        sql = f"""
            SELECT m.id, m.source, m.source_msg_id, m.is_from_me, m.sent_at_utc, m.text,
                   h.person_id AS sender_person_id
              FROM message m
              LEFT JOIN handle h ON h.id = m.sender_handle_id
             WHERE m.is_reaction = 0
               AND m.conversation_id IN ({_ONE_TO_ONE_CONVERSATIONS})
        """
        args: list[object] = [person_id]
        if start_utc is not None:
            sql += " AND m.sent_at_utc >= ?"
            args.append(start_utc)
        if end_utc is not None:
            sql += " AND m.sent_at_utc < ?"
            args.append(end_utc)
        sql += " ORDER BY m.sent_at_utc, m.id"

        self.messages: list[Msg] = []
        for r in store.conn.execute(sql, args):
            if r["is_from_me"]:
                sender = ME
            elif r["sender_person_id"] == person_id:
                sender = THEM
            else:
                continue  # a stray third party in a 1:1 thread; not part of this pair
            self.messages.append(
                Msg(
                    f"{r['source']}:{r['source_msg_id']}",
                    sender,
                    r["sent_at_utc"],
                    r["text"] or "",
                    r["id"],
                )
            )
        self.local: list[datetime] = [
            self.tz.local(self.person_of(m.sender), m.sent_at_utc) for m in self.messages
        ]

    @cached_property
    def segments(self) -> list[list[list[str]]]:
        """Tokenized text per message (text.segments), shared by the language stats."""
        return [segments(m.text) for m in self.messages]

    def person_of(self, sender: str) -> int:
        return ME_PERSON_ID if sender == ME else self.person_id

    def baseline_texts(self) -> list[str]:
        """Texts from my 1:1 conversations with everyone else (distinctive-phrase baseline)."""
        rows = self.store.conn.execute(
            f"""
            SELECT m.text FROM message m
              JOIN conversation c ON c.id = m.conversation_id AND c.is_group = 0
             WHERE m.is_reaction = 0 AND m.text IS NOT NULL
               AND m.conversation_id NOT IN ({_ONE_TO_ONE_CONVERSATIONS})
            """,
            (self.person_id,),
        )
        return [r["text"] for r in rows]
