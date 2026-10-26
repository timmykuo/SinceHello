"""iMessage connector tests against a small synthetic chat.db (no real data)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sincehello.connectors import registry
from sincehello.connectors.imessage import (
    DEFAULT_DB,
    IMessageConnector,
    apple_time,
    is_tapback,
    load_cursors,
    tapback_target,
)

FIXTURES = Path(__file__).parent / "fixtures" / "imessage"
NS = 10**9

CHAT_DB_SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, style INTEGER, chat_identifier TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, guid TEXT UNIQUE, text TEXT, attributedBody BLOB,
    date INTEGER, is_from_me INTEGER, handle_id INTEGER, item_type INTEGER DEFAULT 0,
    associated_message_type INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0,
    date_edited INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0
);
"""

ALEX_PHONE, ALEX_EMAIL, SAM = "+15550001111", "alex@example.com", "+15550002222"


def blob(name: str) -> bytes:
    return (FIXTURES / f"{name}.typedstream").read_bytes()


def add_message(db, rowid, chat_id, guid=None, *, date=700_000_000 * NS, **cols):
    cols = {"is_from_me": 0, "handle_id": 1, **cols}
    names = ", ".join(["ROWID", "guid", "date", *cols])
    marks = ", ".join("?" * (3 + len(cols)))
    db.execute(
        f"INSERT INTO message ({names}) VALUES ({marks})",
        (rowid, guid or f"msg-{rowid}", date, *cols.values()),
    )
    db.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, rowid))


@pytest.fixture
def chat_db(tmp_path) -> Path:
    path = tmp_path / "chat.db"
    db = sqlite3.connect(path)
    db.executescript(CHAT_DB_SCHEMA)
    db.executemany(
        "INSERT INTO handle VALUES (?, ?, ?)",
        [(1, ALEX_PHONE, "iMessage"), (2, ALEX_EMAIL, "iMessage"), (3, SAM, "SMS"),
         (4, ALEX_PHONE, "SMS")],
    )  # fmt: skip
    db.executemany(
        "INSERT INTO chat VALUES (?, ?, ?, ?)",
        [(1, "iMessage;-;+15550001111", 45, ALEX_PHONE),
         (2, "iMessage;-;alex@example.com", 45, ALEX_EMAIL),
         (3, "iMessage;+;chat123", 43, "chat123")],
    )  # fmt: skip
    db.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", [(1, 1), (2, 2), (3, 1), (3, 3)])
    add_message(db, 1, 1, text="hi it's alex")
    add_message(db, 2, 1, is_from_me=1, handle_id=0, text=None, attributedBody=blob("emoji"))
    add_message(db, 3, 1, text=None, attributedBody=blob("non_ascii"))
    add_message(
        db, 4, 1, is_from_me=1, handle_id=0, text='Loved "hi"', associated_message_type=2000
    )
    add_message(db, 5, 1, text="￼", cache_has_attachments=1)
    add_message(db, 6, 1, text=None, attributedBody=blob("attachment_only"))
    add_message(db, 7, 1, text="fixed typo", date_edited=1)
    add_message(db, 8, 2, handle_id=2, text=None, attributedBody=blob("multiline"))
    add_message(db, 9, 3, handle_id=3, text="group hi")
    add_message(db, 10, 3, item_type=2, text=None)  # group renamed
    add_message(db, 11, 1, handle_id=0, text="from alex via another device")
    db.commit()
    db.close()
    return path


def ingest(store, path):
    return IMessageConnector(path).ingest(store)


def rows(store):
    return {
        r["source_msg_id"]: r
        for r in store.conn.execute(
            "SELECT m.*, c.is_group, h.raw_value AS sender FROM message m"
            " JOIN conversation c ON c.id = m.conversation_id"
            " LEFT JOIN handle h ON h.id = m.sender_handle_id"
        )
    }


def test_registered():
    assert isinstance(registry.get("imessage"), IMessageConnector)


def test_apple_time_nanoseconds_and_seconds():
    expected = datetime(2023, 3, 8, 20, 26, 40, tzinfo=UTC)
    assert apple_time(700_000_000 * NS) == expected
    assert apple_time(700_000_000) == expected
    assert apple_time(0) == datetime(2001, 1, 1, tzinfo=UTC)


def test_tapback_ranges():
    assert is_tapback(2000) and is_tapback(3005) and is_tapback(2006)
    assert not is_tapback(0) and not is_tapback(1000) and not is_tapback(None)


def test_ingest_maps_messages(store, chat_db):
    report = ingest(store, chat_db)
    assert report.messages_added == 10  # 11 rows minus one system row
    assert any("system" in w for w in report.warnings)
    got = rows(store)
    assert got["msg-1"]["text"] == "hi it's alex" and got["msg-1"]["sender"] == ALEX_PHONE
    assert got["msg-2"]["text"].startswith("omg yes") and got["msg-2"]["is_from_me"] == 1
    assert got["msg-3"]["text"].startswith("café")
    assert got["msg-4"]["is_reaction"] == 1
    for guid in ("msg-5", "msg-6"):
        assert got[guid]["text"] is None and got[guid]["has_attachment"] == 1
    assert got["msg-7"]["edited"] == 1
    assert got["msg-8"]["sender"] == ALEX_EMAIL and "\n" in got["msg-8"]["text"]
    assert got["msg-9"]["is_group"] == 1 and got["msg-1"]["is_group"] == 0
    assert got["msg-11"]["sender"] == ALEX_PHONE  # 1:1 fallback when handle_id is 0
    assert got["msg-1"]["sent_at_utc"] == int(apple_time(700_000_000 * NS).timestamp() * 1000)


def test_handles_are_not_merged(store, chat_db):
    ingest(store, chat_db)
    raw = [r["raw_value"] for r in store.conn.execute("SELECT raw_value FROM handle")]
    assert sorted(raw) == sorted([ALEX_PHONE, ALEX_EMAIL, SAM])  # SMS duplicate collapses


def test_reingest_is_idempotent_and_incremental(store, chat_db):
    ingest(store, chat_db)
    assert load_cursors(store.get_cursor("imessage")) == {str(chat_db.resolve()): 11}
    assert ingest(store, chat_db).messages_added == 0

    db = sqlite3.connect(chat_db)
    add_message(db, 12, 1, text="new one")
    db.commit()
    db.close()
    report = ingest(store, chat_db)
    assert (report.messages_seen, report.messages_added) == (1, 1)
    assert load_cursors(store.get_cursor("imessage")) == {str(chat_db.resolve()): 12}


def test_reinserted_guid_is_deduped(store, chat_db):
    """iCloud sync can re-insert a message under a new ROWID; the guid keeps it unique."""
    ingest(store, chat_db)
    db = sqlite3.connect(chat_db)
    db.execute("DELETE FROM message WHERE ROWID = 1")
    db.execute("DELETE FROM chat_message_join WHERE message_id = 1")
    add_message(db, 20, 1, guid="msg-1", text="hi it's alex")
    db.commit()
    db.close()
    report = ingest(store, chat_db)
    assert (report.messages_seen, report.messages_added) == (1, 0)


def test_people_counts_one_to_one_only(store, chat_db):
    ingest(store, chat_db)
    by_handle = {p.handles[0]: p.message_count for p in store.people()}
    # chat 1 has 8 messages, one of them a tapback
    assert by_handle[ALEX_PHONE] == 7
    assert by_handle[ALEX_EMAIL] == 1
    assert by_handle[SAM] == 0


def test_check_states(tmp_path, chat_db):
    assert IMessageConnector(chat_db).check().state == "ready"
    assert IMessageConnector(tmp_path / "missing.db").check().state == "needs_setup"
    not_db = tmp_path / "x.db"
    not_db.write_text("hello")
    assert IMessageConnector(not_db).check().state == "error"


def test_check_permission_denied(tmp_path, monkeypatch):
    import builtins

    def deny(*a, **k):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(builtins, "open", deny)
    status = IMessageConnector(tmp_path / "chat.db").check()
    assert status.state == "needs_setup" and "Full Disk Access" in status.detail


def test_db_path_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_IMESSAGE_DB", str(tmp_path / "other.db"))
    assert IMessageConnector().db_path == tmp_path / "other.db"


def test_second_database_has_its_own_cursor(store, chat_db, tmp_path):
    """An old backup has lower ROWIDs; the main DB's cursor must not skip it."""
    ingest(store, chat_db)
    old = tmp_path / "old" / "sms.db"
    old.parent.mkdir()
    db = sqlite3.connect(old)
    db.executescript(CHAT_DB_SCHEMA)
    db.execute("INSERT INTO handle VALUES (1, ?, 'iMessage')", (ALEX_PHONE,))
    db.execute("INSERT INTO chat VALUES (1, 'iMessage;-;+15550001111', 45, ?)", (ALEX_PHONE,))
    db.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
    add_message(db, 1, 1, guid="old-1", date=600_000_000 * NS, text="years ago")
    add_message(db, 2, 1, guid="msg-1", text="hi it's alex")  # also in the main DB
    db.commit()
    db.close()
    report = ingest(store, old)
    assert (report.messages_seen, report.messages_added) == (2, 1)
    assert ingest(store, chat_db).messages_added == 0
    assert set(load_cursors(store.get_cursor("imessage"))) == {
        str(chat_db.resolve()),
        str(old.resolve()),
    }


def test_legacy_integer_cursor_maps_to_default_db():
    assert load_cursors("291512") == {str(DEFAULT_DB.resolve()): 291512}
    assert load_cursors(None) == {}


def test_tapback_target_strips_prefixes():
    assert tapback_target("p:0/ABC-123") == "ABC-123"
    assert tapback_target("p:12/ABC-123") == "ABC-123"
    assert tapback_target("bp:ABC-123") == "ABC-123"
    assert tapback_target("ABC-123") == "ABC-123"
    assert tapback_target(None) is None


def _with_tapbacks(chat_db, *rows):
    db = sqlite3.connect(chat_db)
    db.execute("ALTER TABLE message ADD COLUMN associated_message_guid TEXT")
    db.execute("UPDATE message SET associated_message_guid = 'p:0/msg-1' WHERE ROWID = 4")
    for rowid, kwargs in rows:
        add_message(db, rowid, 1, **kwargs)
    db.commit()
    db.close()


def test_tapbacks_link_to_target_with_kind(store, chat_db):
    _with_tapbacks(
        chat_db,
        (20, dict(text='Laughed at "hi"', associated_message_type=2003,
                  associated_message_guid="bp:msg-3")),
    )  # fmt: skip
    ingest(store, chat_db)
    [mine] = store.reactions_to("imessage", "msg-1")
    assert (mine["reaction_kind"], mine["is_from_me"]) == ("love", 1)
    [theirs] = store.reactions_to("imessage", "msg-3")
    assert (theirs["reaction_kind"], theirs["is_from_me"]) == ("laugh", 0)


def test_removed_tapback_is_dropped(store, chat_db):
    # Same batch: the love on msg-1 (ROWID 4) is undone by ROWID 21.
    _with_tapbacks(
        chat_db,
        (21, dict(is_from_me=1, handle_id=0, text='Removed a heart from "hi"',
                  associated_message_type=3000, associated_message_guid="p:0/msg-1",
                  date=700_000_100 * NS)),
    )  # fmt: skip
    ingest(store, chat_db)
    assert store.reactions_to("imessage", "msg-1") == []
    assert "msg-21" not in rows(store)


def test_removed_tapback_in_later_ingest(store, chat_db):
    _with_tapbacks(chat_db)
    ingest(store, chat_db)
    assert len(store.reactions_to("imessage", "msg-1")) == 1
    db = sqlite3.connect(chat_db)
    add_message(db, 22, 1, is_from_me=1, handle_id=0, associated_message_type=3000,
                associated_message_guid="p:0/msg-1", date=700_000_100 * NS)  # fmt: skip
    db.commit()
    db.close()
    ingest(store, chat_db)
    assert store.reactions_to("imessage", "msg-1") == []
