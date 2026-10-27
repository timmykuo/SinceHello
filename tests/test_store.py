from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeConnector, default_messages, msg

from sincehello.connectors import Connector, MessageRecord
from sincehello.store import Store
from sincehello.store.store import SCHEMA_VERSION


def test_fake_connector_satisfies_protocol():
    assert isinstance(FakeConnector(), Connector)


def test_ingest_is_idempotent(store):
    first = FakeConnector().ingest(store)
    assert (first.messages_seen, first.messages_added) == (5, 5)
    store.set_cursor("fake", "0")  # force a full re-read
    again = FakeConnector().ingest(store)
    assert (again.messages_seen, again.messages_added) == (5, 0)


def test_cursor_limits_reingest(store):
    FakeConnector().ingest(store)
    assert store.get_cursor("fake") == "5"
    report = FakeConnector().ingest(store)
    assert report.messages_seen == 0


def test_people_counts_one_to_one_only_and_skips_reactions(store):
    FakeConnector().ingest(store)
    people = {p.handles[0]: p for p in store.people()}
    assert people["+15550001111"].display_name == "Alex"
    assert people["+15550001111"].message_count == 3  # group message and reaction excluded
    assert people["+15550002222"].message_count == 0


def test_me_is_not_listed(store):
    FakeConnector().ingest(store)
    assert all(p.person_id != 1 for p in store.people())


def test_fts_search(store):
    FakeConnector().ingest(store)
    hits = store.search('"love you"')
    assert [h["source_msg_id"] for h in hits] == ["3", "4"]


def test_naive_timestamp_rejected(store):
    FakeConnector(messages=[]).ingest(store)
    bad = MessageRecord("fake", "9", "c-alex", True, datetime(2021, 1, 1), "hi")
    with pytest.raises(ValueError, match="timezone-aware"):
        store.add_messages([bad])


def test_unknown_conversation_rejected(store):
    with pytest.raises(ValueError, match="unknown conversation"):
        store.add_messages([msg(1, "nope", True, "hi")])


def test_reopen_keeps_data(tmp_path):
    path = tmp_path / "s.db"
    with Store(path) as s:
        FakeConnector().ingest(s)
    with Store(path) as s:
        assert len(s.people()) == 2


def test_reaction_links_to_target(store):
    FakeConnector().ingest(store)
    [reaction] = store.reactions_to("fake", "3")
    assert reaction["source_msg_id"] == "4"
    assert reaction["reaction_kind"] == "love"
    assert store.reactions_to("fake", "1") == []


def test_reaction_fields_require_is_reaction(store):
    FakeConnector(messages=[]).ingest(store)
    bad = msg(9, "c-alex", True, "hi", reacts_to="1")
    with pytest.raises(ValueError, match="is_reaction=False"):
        store.add_messages([bad])


def test_migrates_v1_store(tmp_path):
    path = tmp_path / "old.db"
    with Store(path) as s:
        FakeConnector(messages=default_messages()[:3]).ingest(s)
        # Turn this into a v1 store: no reaction columns, no tz_suggestion.
        s.conn.executescript(
            """
            DROP TABLE tz_suggestion;
            DROP INDEX message_reacts_to;
            ALTER TABLE message DROP COLUMN reacts_to_source_msg_id;
            ALTER TABLE message DROP COLUMN reaction_kind;
            PRAGMA user_version = 1;
            """
        )
    with Store(path) as s:
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert s.people()[0].message_count == 3  # data survives
        s.set_cursor("fake", "0")
        FakeConnector().ingest(s)
        assert s.reactions_to("fake", "3")[0]["reaction_kind"] == "love"


def test_migrates_v2_store(tmp_path):
    path = tmp_path / "v2.db"
    with Store(path) as s:
        FakeConnector(messages=default_messages()[:3]).ingest(s)
        s.conn.executescript("DROP TABLE tz_suggestion; PRAGMA user_version = 2;")
    with Store(path) as s:
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        sid = s.add_tz_suggestion(
            1,
            kind="shift",
            start_utc=0,
            end_utc=None,
            utc_offset_hours=9.0,
            iana_tz="Etc/GMT-9",
            confidence="high",
            score=0.9,
        )
        assert s.tz_suggestions(1, "pending")[0]["id"] == sid


def test_newer_schema_rejected(tmp_path):
    path = tmp_path / "future.db"
    with Store(path) as s:
        s.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer than this code"):
        Store(path)
