from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeConnector, msg

from sincehello.connectors import Connector, MessageRecord
from sincehello.store import Store


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
