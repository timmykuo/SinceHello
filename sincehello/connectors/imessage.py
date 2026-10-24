"""iMessage connector (SPEC.md §4.2): reads a snapshot of ~/Library/Messages/chat.db."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sincehello.connectors.base import (
    ConnectorStatus,
    ConversationRecord,
    HandleRecord,
    IngestReport,
    MessageRecord,
    write_batch,
)
from sincehello.connectors.imessage_text import try_decode

if TYPE_CHECKING:
    from sincehello.store import Store

SOURCE = "imessage"
DB_ENV = "SINCEHELLO_IMESSAGE_DB"  # read another chat.db (old Mac) or sms.db (iPhone backup)
DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
ATTACHMENT_CHAR = "￼"
# chat.style values; anything else falls back to the member count.
STYLE_GROUP = 43
STYLE_DIRECT = 45


def is_tapback(associated_message_type: int | None) -> bool:
    # 2000-2005 classic tapbacks, 2006 emoji reaction, 2007 sticker; 3xxx removes them.
    t = associated_message_type or 0
    return 2000 <= t <= 2007 or 3000 <= t <= 3007


def apple_time(value: int) -> datetime:
    """chat.db dates: nanoseconds since 2001-01-01 UTC, or seconds on older databases."""
    if abs(value) > 10**11:
        return APPLE_EPOCH + timedelta(microseconds=value // 1000)
    return APPLE_EPOCH + timedelta(seconds=value)


def clean_text(text: str | None) -> tuple[str | None, bool]:
    """Strip attachment placeholders. Returns (text or None if nothing left, had_attachment)."""
    if text is None:
        return None, False
    had_attachment = ATTACHMENT_CHAR in text
    stripped = text.replace(ATTACHMENT_CHAR, "").strip()
    return (stripped or None), had_attachment


class IMessageConnector:
    id = SOURCE
    kind = "automatic"

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or os.environ.get(DB_ENV) or DEFAULT_DB).expanduser()

    def check(self) -> ConnectorStatus:
        # Read the header with plain file I/O so check() never opens the live DB in SQLite.
        try:
            with open(self.db_path, "rb") as f:
                header = f.read(16)
        except PermissionError:
            return ConnectorStatus("needs_setup", "Full Disk Access is required to read Messages.")
        except FileNotFoundError:
            return ConnectorStatus("needs_setup", f"No Messages database at {self.db_path}.")
        if header != b"SQLite format 3\x00":
            return ConnectorStatus("error", f"{self.db_path} is not a SQLite database.")
        return ConnectorStatus("ready", str(self.db_path))

    def setup_instructions(self) -> list[str]:
        return [
            "Open System Settings → Privacy & Security → Full Disk Access.",
            "Turn on the app you run sincehello from (Terminal, iTerm, VS Code, Claude, ...),"
            " adding it with + if it isn't listed.",
            "Quit and reopen that app; the permission only applies to new processes.",
            "Run `sincehello connect imessage` again.",
            f"To read a database copied from elsewhere, set {DB_ENV}=/path/to/chat.db.",
        ]

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """A consistent read-only copy of the database, taken with SQLite's backup API.

        Messages may be writing (e.g. during an iCloud sync), so a file copy could catch the
        database mid-write. The live file is only opened read-only, as the backup source.
        """
        with tempfile.TemporaryDirectory(prefix="sincehello-imessage-") as tmp:
            copy = Path(tmp) / "chat.db"
            src = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(copy)
                with dst:
                    src.backup(dst)
                dst.close()
            finally:
                src.close()
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    def ingest(self, store: Store) -> IngestReport:
        cursor = int(store.get_cursor(self.id) or 0)
        with self.snapshot() as db:
            reader = _Reader(db)
            report = write_batch(
                store,
                self.id,
                handles=reader.handles(),
                conversations=reader.conversations(),
                messages=reader.messages(after_rowid=cursor),
            )
            report.warnings.extend(reader.warnings())
            if reader.max_rowid > cursor:
                store.set_cursor(self.id, str(reader.max_rowid))
        return report


class _Reader:
    """Turns chat.db rows into records. Tracks skips and the max ROWID it read."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.max_rowid = 0
        self.skipped_system = 0
        self.skipped_no_chat = 0
        self.skipped_no_sender = 0
        self.handle_by_rowid = {
            r["ROWID"]: r["id"] for r in db.execute("SELECT ROWID, id FROM handle")
        }
        self.members: dict[int, list[str]] = {}
        for r in db.execute("SELECT chat_id, handle_id FROM chat_handle_join ORDER BY handle_id"):
            raw = self.handle_by_rowid.get(r["handle_id"])
            if raw is not None and raw not in self.members.setdefault(r["chat_id"], []):
                self.members[r["chat_id"]].append(raw)
        self.chat_guid: dict[int, str] = {}
        self.chat_is_group: dict[int, bool] = {}
        for r in db.execute("SELECT ROWID, guid, style FROM chat"):
            members = self.members.get(r["ROWID"], [])
            if r["style"] == STYLE_GROUP:
                is_group = True
            elif r["style"] == STYLE_DIRECT:
                is_group = False
            else:
                is_group = len(members) > 1
            self.chat_guid[r["ROWID"]] = r["guid"]
            self.chat_is_group[r["ROWID"]] = is_group
        columns = {r["name"] for r in db.execute("PRAGMA table_info(message)")}
        self._edited_expr = (
            " OR ".join(
                f"coalesce(m.{c}, 0) > 0" for c in ("date_edited", "date_retracted") if c in columns
            )
            or "0"
        )
        self._body_expr = "m.attributedBody" if "attributedBody" in columns else "NULL"

    def handles(self) -> Iterator[HandleRecord]:
        for raw in dict.fromkeys(self.handle_by_rowid.values()):
            yield HandleRecord(SOURCE, raw)

    def conversations(self) -> Iterator[ConversationRecord]:
        for rowid, guid in self.chat_guid.items():
            yield ConversationRecord(
                SOURCE, guid, self.chat_is_group[rowid], tuple(self.members.get(rowid, ()))
            )

    def messages(self, after_rowid: int) -> Iterator[MessageRecord]:
        self.max_rowid = after_rowid
        rows = self.db.execute(
            f"""
            SELECT m.ROWID, m.guid, m.text, {self._body_expr} AS body, m.date, m.is_from_me,
                   m.handle_id, m.item_type, m.associated_message_type,
                   m.cache_has_attachments, ({self._edited_expr}) AS edited,
                   (SELECT min(chat_id) FROM chat_message_join WHERE message_id = m.ROWID)
                       AS chat_id
              FROM message m
             WHERE m.ROWID > ?
             ORDER BY m.ROWID
            """,
            (after_rowid,),
        )
        for r in rows:
            self.max_rowid = max(self.max_rowid, r["ROWID"])
            if r["item_type"]:  # group renames, member changes, and other system rows
                self.skipped_system += 1
                continue
            chat_id = r["chat_id"]
            if chat_id not in self.chat_guid:
                self.skipped_no_chat += 1
                continue
            sender = None
            if not r["is_from_me"]:
                sender = self.handle_by_rowid.get(r["handle_id"])
                members = self.members.get(chat_id, [])
                if sender is None and len(members) == 1:
                    sender = members[0]
                if sender is None:
                    self.skipped_no_sender += 1
                    continue
            raw_text = r["text"] if r["text"] is not None else try_decode(r["body"])
            text, placeholder = clean_text(raw_text)
            yield MessageRecord(
                source=SOURCE,
                source_msg_id=r["guid"],
                source_conv_id=self.chat_guid[chat_id],
                is_from_me=bool(r["is_from_me"]),
                sent_at_utc=apple_time(r["date"] or 0),
                text=text,
                sender=sender,
                is_reaction=is_tapback(r["associated_message_type"]),
                has_attachment=bool(r["cache_has_attachments"]) or placeholder,
                edited=bool(r["edited"]),
            )

    def warnings(self) -> list[str]:
        out = []
        if self.skipped_system:
            out.append(f"skipped {self.skipped_system} system messages (renames, joins, ...)")
        if self.skipped_no_chat:
            out.append(f"skipped {self.skipped_no_chat} messages not linked to any chat")
        if self.skipped_no_sender:
            out.append(f"skipped {self.skipped_no_sender} incoming messages with no sender")
        return out
