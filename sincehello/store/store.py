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

SCHEMA_VERSION = 3

# version -> SQL that upgrades a store from that version to the next one.
# schema.sql always holds the latest full schema, used for new stores.
MIGRATIONS = {
    1: """
        ALTER TABLE message ADD COLUMN reacts_to_source_msg_id TEXT;
        ALTER TABLE message ADD COLUMN reaction_kind TEXT;
        CREATE INDEX message_reacts_to ON message (source, reacts_to_source_msg_id);
    """,
    2: """
        CREATE TABLE tz_suggestion (
            id               INTEGER PRIMARY KEY,
            person_id        INTEGER NOT NULL REFERENCES person(id),
            kind             TEXT NOT NULL,
            start_utc        INTEGER NOT NULL,
            end_utc          INTEGER,
            utc_offset_hours REAL NOT NULL,
            iana_tz          TEXT NOT NULL,
            confidence       TEXT NOT NULL,
            score            REAL NOT NULL,
            place            TEXT,
            hint_message_id  TEXT,
            hint_quote       TEXT,
            details          TEXT NOT NULL DEFAULT '{}',
            status           TEXT NOT NULL DEFAULT 'pending'
        );
    """,
}
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
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"store schema v{version} is newer than this code (v{SCHEMA_VERSION})"
            )
        with self.conn:
            if version == 0:
                sql = resources.files("sincehello.store").joinpath("schema.sql").read_text()
                self.conn.executescript(sql)
            else:
                for v in range(version, SCHEMA_VERSION):
                    self.conn.executescript(MIGRATIONS[v])
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
                if not m.is_reaction and (m.reacts_to or m.reaction_kind):
                    raise ValueError(
                        f"message {m.source_msg_id} has reaction fields but is_reaction=False"
                    )
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
                    " has_attachment, edited, reacts_to_source_msg_id, reaction_kind)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        m.reacts_to,
                        m.reaction_kind,
                    ),
                )
                added += cur.rowcount
        return seen, added

    def remove_reaction(
        self,
        source: str,
        reacts_to: str,
        reaction_kind: str,
        is_from_me: bool,
        sender: str | None,
        before: datetime,
    ) -> bool:
        """Delete the latest matching reaction sent before `before` (the reaction was undone).

        Returns False when there's nothing to remove, e.g. the reaction was never ingested.
        """
        sender_id = None if is_from_me else self._handle_id(source, sender or "")
        if not is_from_me and sender_id is None:
            return False
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM message WHERE id = (SELECT id FROM message"
                " WHERE source = ? AND is_reaction = 1 AND reacts_to_source_msg_id = ?"
                " AND reaction_kind = ? AND is_from_me = ? AND sender_handle_id IS ?"
                " AND sent_at_utc <= ? ORDER BY sent_at_utc DESC LIMIT 1)",
                (source, reacts_to, reaction_kind, int(is_from_me), sender_id, to_utc_ms(before)),
            )
        return cur.rowcount > 0

    def resolve_identities(self) -> int:
        """Run the automatic merges (SPEC.md §6). Returns how many handles moved."""
        from sincehello.store.identity import auto_merge

        return auto_merge(self)

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

    def add_tz_period(
        self,
        person_id: int,
        start_utc: datetime,
        end_utc: datetime | None,
        iana_tz: str,
        source: str = "user",
    ) -> int:
        """Record that a person was in iana_tz from start_utc until end_utc (SPEC.md §6.1)."""
        with self.conn:
            return self.conn.execute(
                "INSERT INTO tz_period (person_id, start_utc, end_utc, iana_tz, source)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    person_id,
                    to_utc_ms(start_utc),
                    to_utc_ms(end_utc) if end_utc else None,
                    iana_tz,
                    source,
                ),
            ).lastrowid

    _SUGGESTION_FIELDS = (
        "kind",
        "start_utc",
        "end_utc",
        "utc_offset_hours",
        "iana_tz",
        "confidence",
        "score",
        "place",
        "hint_message_id",
        "hint_quote",
        "details",
        "status",
    )

    def add_tz_suggestion(self, person_id: int, **fields: object) -> int:
        """Store an inferred timezone period (SPEC.md §6.1). Times are UTC ms."""
        cols = [c for c in self._SUGGESTION_FIELDS if c in fields]
        with self.conn:
            return self.conn.execute(
                f"INSERT INTO tz_suggestion (person_id, {', '.join(cols)})"
                f" VALUES (?, {', '.join('?' * len(cols))})",
                (person_id, *(fields[c] for c in cols)),
            ).lastrowid

    def update_tz_suggestion(self, suggestion_id: int, **fields: object) -> None:
        unknown = set(fields) - set(self._SUGGESTION_FIELDS)
        if unknown:
            raise ValueError(f"unknown tz_suggestion fields: {sorted(unknown)}")
        if not fields:
            return
        with self.conn:
            self.conn.execute(
                f"UPDATE tz_suggestion SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                (*fields.values(), suggestion_id),
            )

    def delete_tz_suggestion(self, suggestion_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM tz_suggestion WHERE id = ?", (suggestion_id,))

    # --- reads --------------------------------------------------------------

    def person_for_handle(self, source: str, raw_value: str) -> int | None:
        row = self.conn.execute(
            "SELECT person_id FROM handle WHERE source = ? AND raw_value = ?", (source, raw_value)
        ).fetchone()
        return row["person_id"] if row else None

    def tz_periods(self, person_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tz_period WHERE person_id = ? ORDER BY start_utc, id", (person_id,)
        ).fetchall()

    def tz_suggestions(self, person_id: int, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM tz_suggestion WHERE person_id = ?"
        args: list[object] = [person_id]
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        return self.conn.execute(sql + " ORDER BY start_utc, id", args).fetchall()

    def people(self) -> list[PersonSummary]:
        """Everyone except me, by message count in 1:1 conversations (SPEC.md §1).

        A person's count covers every 1:1 conversation with any of their handles.
        """
        rows = self.conn.execute(
            """
            WITH conv_count AS (
                SELECT m.conversation_id, count(*) AS n FROM message m
                  JOIN conversation c ON c.id = m.conversation_id AND c.is_group = 0
                 WHERE m.is_reaction = 0
                 GROUP BY m.conversation_id
            ), person_conv AS (
                SELECT DISTINCT h.person_id, cm.conversation_id FROM conversation_member cm
                  JOIN handle h ON h.id = cm.handle_id
            )
            SELECT p.id, p.display_name,
                   (SELECT group_concat(raw_value, ', ') FROM handle WHERE person_id = p.id)
                       AS handles,
                   coalesce(sum(cc.n), 0) AS message_count
              FROM person p
              LEFT JOIN person_conv pc ON pc.person_id = p.id
              LEFT JOIN conv_count cc ON cc.conversation_id = pc.conversation_id
             WHERE p.is_me = 0
             GROUP BY p.id
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

    def reactions_to(self, source: str, source_msg_id: str) -> list[sqlite3.Row]:
        """Reactions pointing at one message, oldest first."""
        return self.conn.execute(
            "SELECT * FROM message WHERE source = ? AND reacts_to_source_msg_id = ?"
            " ORDER BY sent_at_utc",
            (source, source_msg_id),
        ).fetchall()

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
