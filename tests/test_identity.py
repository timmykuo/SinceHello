from __future__ import annotations

import pytest
from conftest import FakeConnector, msg
from typer.testing import CliRunner

from sincehello.cli import app
from sincehello.connectors import ConversationRecord, HandleRecord, registry, write_batch
from sincehello.store import identity
from sincehello.store.identity import IdentityError, normalize_phone

ALEX_PHONE = "+15550001111"
ALEX_EMAIL = "alex@example.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+15550001111", "+15550001111"),
        ("+1 (555) 000-1111", "+15550001111"),
        ("(555) 000-1111", "+15550001111"),
        ("555.000.1111", "+15550001111"),
        ("15550001111", "+15550001111"),
        ("+44 20 7946 0000", "+442079460000"),
        ("alex@example.com", None),
        ("12345", None),  # short code
        ("5550001111x2", None),
        ("0555000111", None),
        ("+0123456789", None),
        ("555+0001111", None),
        ("", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def ingest(store, handles, conversations, messages):
    return write_batch(store, "fake", handles, conversations, messages)


def alex_two_handles(store, email_name=None):
    """Alex texts from a phone number in one 1:1 chat and an email in another."""
    ingest(
        store,
        [HandleRecord("fake", ALEX_PHONE, "Alex"), HandleRecord("fake", ALEX_EMAIL, email_name)],
        [
            ConversationRecord("fake", "c-phone", False, (ALEX_PHONE,)),
            ConversationRecord("fake", "c-email", False, (ALEX_EMAIL,)),
        ],
        [
            msg(1, "c-phone", False, "hi"),
            msg(2, "c-phone", True, "hey"),
            msg(3, "c-email", False, "from my laptop", sender=ALEX_EMAIL),
        ],
    )


def person_of(store, raw):
    return identity.find_handle(store, raw).person_id


def test_merge_combines_counts_and_deletes_empty_person(store):
    alex_two_handles(store)
    assert len(store.people()) == 2
    email_person = person_of(store, ALEX_EMAIL)
    h = identity.find_handle(store, ALEX_EMAIL)
    left = identity.merge_handle(store, person_of(store, ALEX_PHONE), h.id)
    assert left == email_person
    (alex,) = store.people()
    assert alex.display_name == "Alex"
    assert set(alex.handles) == {ALEX_PHONE, ALEX_EMAIL}
    assert alex.message_count == 3
    assert not store.conn.execute("SELECT 1 FROM person WHERE id = ?", (email_person,)).fetchone()


def test_merge_keeps_name_from_merged_person(store):
    ingest(store, [HandleRecord("fake", ALEX_PHONE)], [], [])
    ingest(store, [HandleRecord("fake", ALEX_EMAIL, "Alex")], [], [])
    h = identity.find_handle(store, ALEX_EMAIL)
    identity.merge_handle(store, person_of(store, ALEX_PHONE), h.id)
    (alex,) = store.people()
    assert alex.display_name == "Alex"


def test_merge_is_noop_when_already_merged(store):
    alex_two_handles(store)
    h = identity.find_handle(store, ALEX_PHONE)
    assert identity.merge_handle(store, h.person_id, h.id) is None


def test_merge_unknown_person_or_handle(store):
    alex_two_handles(store)
    h = identity.find_handle(store, ALEX_PHONE)
    with pytest.raises(IdentityError, match="no person"):
        identity.merge_handle(store, 999, h.id)
    with pytest.raises(IdentityError, match="no handle"):
        identity.find_handle(store, "nobody@example.com")


def test_find_handle_by_id_and_normalized_phone(store):
    alex_two_handles(store)
    h = identity.find_handle(store, ALEX_PHONE)
    assert identity.find_handle(store, str(h.id)) == h
    assert identity.find_handle(store, "(555) 000-1111") == h


def test_find_handle_ambiguous_across_sources(store):
    write_batch(store, "fake", [HandleRecord("fake", ALEX_EMAIL)], [], [])
    write_batch(store, "other", [HandleRecord("other", ALEX_EMAIL)], [], [])
    with pytest.raises(IdentityError, match="several handles"):
        identity.find_handle(store, ALEX_EMAIL)


def test_auto_merge_same_phone_number(store):
    ingest(
        store,
        [HandleRecord("fake", ALEX_PHONE, "Alex"), HandleRecord("fake", "5550001111")],
        [
            ConversationRecord("fake", "c-sms", False, ("5550001111",)),
            ConversationRecord("fake", "c-imsg", False, (ALEX_PHONE,)),
        ],
        [msg(1, "c-sms", False, "sms", sender="5550001111"), msg(2, "c-imsg", False, "imsg")],
    )
    (alex,) = store.people()
    assert set(alex.handles) == {ALEX_PHONE, "5550001111"}
    assert alex.message_count == 2
    assert store.resolve_identities() == 0  # idempotent


def test_auto_merge_leaves_different_numbers_and_emails_alone(store):
    alex_two_handles(store)
    ingest(store, [HandleRecord("fake", "+15550002222")], [], [])
    assert len(store.people()) == 3


def test_one_to_one_chat_with_two_handles_of_one_person_counts_once(store):
    ingest(
        store,
        [HandleRecord("fake", ALEX_PHONE), HandleRecord("fake", ALEX_EMAIL)],
        [ConversationRecord("fake", "c", False, (ALEX_PHONE, ALEX_EMAIL))],
        [msg(1, "c", False, "hi"), msg(2, "c", True, "hey")],
    )
    identity.merge_handle(
        store, person_of(store, ALEX_PHONE), identity.find_handle(store, ALEX_EMAIL).id
    )
    (alex,) = store.people()
    assert alex.message_count == 2


def test_mark_me_hides_handle(store):
    ingest(store, [HandleRecord("fake", "me@example.com")], [], [])
    alex_two_handles(store)
    identity.mark_me(store, identity.find_handle(store, "me@example.com").id)
    assert {p.handles for p in store.people()} == {(ALEX_PHONE,), (ALEX_EMAIL,)}
    assert person_of(store, "me@example.com") == 1


def test_auto_merge_prefers_me(store):
    ingest(store, [HandleRecord("fake", "+15550009999")], [], [])
    identity.mark_me(store, identity.find_handle(store, "+15550009999").id)
    ingest(store, [HandleRecord("fake", "5550009999")], [], [])
    assert person_of(store, "5550009999") == 1
    assert store.people() == []


def test_manual_merge_moves_same_number_handles_together(store):
    ingest(store, [HandleRecord("fake", ALEX_PHONE), HandleRecord("fake", "5550001111")], [], [])
    ingest(store, [HandleRecord("fake", ALEX_EMAIL)], [], [])
    h = identity.find_handle(store, ALEX_PHONE)
    identity.merge_handle(store, person_of(store, ALEX_EMAIL), h.id)
    assert store.resolve_identities() == 0
    (alex,) = store.people()
    assert set(alex.handles) == {ALEX_PHONE, "5550001111", ALEX_EMAIL}


# --- CLI ----------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path))
    monkeypatch.setitem(registry.CONNECTORS, "fake", FakeConnector)
    assert runner.invoke(app, ["connect", "fake"]).exit_code == 0


def test_cli_merge_by_handles(cli_home):
    result = runner.invoke(app, ["merge", "+15550001111", "+15550002222"])
    assert result.exit_code == 0, result.output
    assert "Moved +15550002222" in result.output
    lines = runner.invoke(app, ["people"]).output.strip().splitlines()
    assert len(lines) == 1 and "+15550002222" in lines[0]


def test_cli_merge_unknown(cli_home):
    result = runner.invoke(app, ["merge", "+15550001111", "nobody@example.com"])
    assert result.exit_code == 1
    assert "no handle" in result.output


def test_cli_me(cli_home):
    result = runner.invoke(app, ["me", "+15550002222"])
    assert result.exit_code == 0, result.output
    assert "+15550002222" not in runner.invoke(app, ["people"]).output
