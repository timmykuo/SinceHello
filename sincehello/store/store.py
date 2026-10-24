"""SQLite-backed normalized store (SPEC.md §5)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path

from sincehello.connectors.base import ConversationRecord, HandleRecord, MessageRecord

SCHEMA_VERSION = 1
ME_PERSON_ID = 1


@dataclass(frozen=True)
class PersonSummary:
    person_id: int
    display_name: str | None
    handles: tuple[str, ...]
    message_count: int  # 1:1 conversations only, reactions excluded


def to_utc_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(dt.timestamp() * 1000)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        (version,) = self.conn.execute("PRAGMA user_version").fetchone()
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise RuntimeError(f"unsupported schema version {version}")
        sql = resources.files("sincehello.store").joinpath("schema.sql").read_text()
        with self.conn:
            self.conn.executescript(sql)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # --- writes -------------------------------------------------------------

    def upsert_handle(self, h: HandleRecord) -> int:
        row = self.conn.execute(
            "SELECT id, display_name FROM handle WHERE source = ? AND raw_value = ?",
            (h.source, h.raw_value),
        ).fetchone()
        with self.conn:
            if row:
                if h.display_name and h.display_name != row["display_name"]:
                    self.conn.execute(
                        "UPDATE handle SET display_name = ? WHERE id = ?",
                        (h.display_name, row["id"]),
                    )
                return row["id"]
            # Each new handle starts as its own person; identity resolution merges later.
            person_id = self.conn.execute(
                "INSERT INTO person (display_name) VALUES (?)", (h.display_name,)
            ).lastrowid
            return self.conn.execute(
                "INSERT INTO handle (person_id, source, raw_value, display_name)"
                " VALUES (?, ?, ?, ?)",
                (person_id, h.source, h.raw_value, h.display_name),
            ).lastrowid

    def upsert_conversation(self, c: ConversationRecord) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT INTO conversation (source, source_conv_id, is_group) VALUES (?, ?, ?)"
                " ON CONFLICT (source, source_conv_id) DO UPDATE SET is_group = excluded.is_group",
                (c.source, c.source_conv_id, int(c.is_group)),
            )
            conv_id = self._conversation_id(c.source, c.source_conv_id)
            for raw in c.member_handles:
                handle_id = self._handle_id(c.source, raw)
                if handle_id is None:
                    handle_id = self.upsert_handle(HandleRecord(c.source, raw))
                self.conn.execute(
                    "INSERT OR IGNORE INTO conversation_member VALUES (?, ?)",
                    (conv_id, handle_id),
                )
        return conv_id

    def add_messages(self, messages: Iterable[MessageRecord]) -> tuple[int, int]:
        """Insert messages, skipping ones already stored. Returns (seen, added)."""
        conv_ids: dict[tuple[str, str], int] = {}
        handle_ids: dict[tuple[str, str], int] = {}
        seen = added = 0
        with self.conn:
            for m in messages:
                seen += 1
                ck = (m.source, m.source_conv_id)
                if ck not in conv_ids:
                    conv_id = self._conversation_id(*ck)
                    if conv_id is None:
                        raise ValueError(f"unknown conversation {ck}; upsert it first")
                    conv_ids[ck] = conv_id
                sender_id = None
                if not m.is_from_me:
                    if m.sender is None:
                        raise ValueError(f"message {m.source_msg_id} has no sender")
                    hk = (m.source, m.sender)
                    if hk not in handle_ids:
                        handle_ids[hk] = self._handle_id(*hk) or self.upsert_handle(
                            HandleRecord(m.source, m.sender)
                        )
                    sender_id = handle_ids[hk]
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO message (source, source_msg_id, conversation_id,"
                    " sender_handle_id, is_from_me, sent_at_utc, text, is_reaction,"
                    " has_attachment, edited) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        m.source,
                        m.source_msg_id,
                        conv_ids[ck],
                        sender_id,
                        int(m.is_from_me),
                        to_utc_ms(m.sent_at_utc),
                        m.text,
                        int(m.is_reaction),
                        int(m.has_attachment),
                        int(m.edited),
                    ),
                )
                added += cur.rowcount
        return seen, added

    def get_cursor(self, source: str) -> str | None:
        row = self.conn.execute("SELECT cursor FROM sync_state WHERE source = ?", (source,))
        found = row.fetchone()
        return found["cursor"] if found else None

    def set_cursor(self, source: str, cursor: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sync_state (source, cursor, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (source) DO UPDATE SET cursor = excluded.cursor,"
                " updated_at = excluded.updated_at",
                (source, cursor, int(time.time() * 1000)),
            )

    # --- reads --------------------------------------------------------------

    def people(self) -> list[PersonSummary]:
        """Everyone except me, by message count in 1:1 conversations (SPEC.md §1)."""
        rows = self.conn.execute(
            """
            SELECT p.id, p.display_name,
                   (SELECT group_concat(raw_value, ', ') FROM handle WHERE person_id = p.id)
                       AS handles,
                   (SELECT count(*) FROM message m
                      JOIN conversation c ON c.id = m.conversation_id AND c.is_group = 0
                      JOIN conversation_member cm ON cm.conversation_id = c.id
                      JOIN handle h ON h.id = cm.handle_id AND h.person_id = p.id
                     WHERE m.is_reaction = 0) AS message_count
              FROM person p
             WHERE p.is_me = 0
             ORDER BY message_count DESC, p.id
            """
        ).fetchall()
        return [
            PersonSummary(
                person_id=r["id"],
                display_name=r["display_name"],
                handles=tuple(r["handles"].split(", ")) if r["handles"] else (),
                message_count=r["message_count"],
            )
            for r in rows
        ]

    def search(self, query: str, limit: int = 25) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT m.* FROM message_fts f JOIN message m ON m.id = f.rowid"
            " WHERE message_fts MATCH ? ORDER BY m.sent_at_utc LIMIT ?",
            (query, limit),
        ).fetchall()

    # --- helpers ------------------------------------------------------------

    def _conversation_id(self, source: str, source_conv_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM conversation WHERE source = ? AND source_conv_id = ?",
            (source, source_conv_id),
        ).fetchone()
        return row["id"] if row else None

    def _handle_id(self, source: str, raw_value: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM handle WHERE source = ? AND raw_value = ?", (source, raw_value)
        ).fetchone()
        return row["id"] if row else None
