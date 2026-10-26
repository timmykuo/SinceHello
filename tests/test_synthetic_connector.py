from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from evals import generator
from sincehello.cli import app
from sincehello.connectors import Connector, registry
from sincehello.connectors.synthetic import ENV_DIR, SAMPLE_DIR, SyntheticConnector

runner = CliRunner()


@pytest.fixture(scope="module")
def sample_manifest():
    return json.loads((SAMPLE_DIR / "manifest.json").read_text())


def test_satisfies_protocol_and_is_registered():
    assert isinstance(SyntheticConnector(), Connector)
    assert registry.get("synthetic").id == "synthetic"


def test_default_points_at_the_sample(monkeypatch):
    monkeypatch.delenv(ENV_DIR, raising=False)
    status = SyntheticConnector().check()
    assert status.state == "ready" and "bean-goose-s42" in status.detail


def test_missing_dir_needs_setup(tmp_path):
    c = SyntheticConnector(tmp_path)
    assert c.check().state == "needs_setup"
    assert c.setup_instructions()


def test_ingest_sample(store, sample_manifest):
    report = SyntheticConnector(SAMPLE_DIR).ingest(store)
    stats = sample_manifest["stats"]
    assert report.messages_added == report.messages_seen == stats["lines"]
    assert report.conversations == 2
    people = {p.display_name: p for p in store.people()}
    alex = people[sample_manifest["participants"]["them"]["name"]]
    # 1:1 only, reactions excluded: exactly the manifest's 1:1 count.
    assert alex.message_count == stats["dm_messages"]
    assert people["Jules Okafor"].message_count == 0  # only in the group chat
    c = store.conn
    assert (
        c.execute("SELECT count(*) FROM message WHERE is_reaction = 1").fetchone()[0]
        == (stats["dm_reactions"])
    )
    assert (
        c.execute(
            "SELECT count(*) FROM message m JOIN conversation v ON v.id = m.conversation_id"
            " WHERE v.is_group = 1"
        ).fetchone()[0]
        == stats["group_messages"]
    )
    assert (
        c.execute("SELECT count(*) FROM message WHERE text IS NULL").fetchone()[0]
        == (stats["attachment_only"])
    )
    assert (
        c.execute("SELECT count(*) FROM message WHERE edited = 1").fetchone()[0]
        == (stats["edited"])
    )


def test_ids_and_timestamps_match_the_manifest(store, sample_manifest):
    SyntheticConnector(SAMPLE_DIR).ingest(store)
    ily = next(f for f in sample_manifest["facts"] if f["id"] == "first_ily")
    row = store.conn.execute(
        "SELECT * FROM message WHERE source = 'synthetic' AND source_msg_id = ?",
        (ily["message_id"],),
    ).fetchone()
    assert row["text"] == "i love you" and row["is_from_me"] == 0
    assert row["sent_at_utc"] == 1621050480000  # 2021-05-15T03:48:00Z in ms
    first = store.search('"love you"', limit=1)[0]
    assert first["source_msg_id"] != ily["message_id"]  # the group-chat trap comes first


def test_ingest_is_idempotent(store):
    SyntheticConnector(SAMPLE_DIR).ingest(store)
    again = SyntheticConnector(SAMPLE_DIR).ingest(store)
    assert again.messages_added == 0 and again.messages_seen > 0
    assert store.conn.execute("SELECT count(*) FROM person").fetchone()[0] == 4  # me + 3


def test_ingest_a_generated_dir(store, tmp_path):
    records, manifest = generator.generate("noodle-summit", seed=3, target_messages=1200)
    generator.write(tmp_path, records, manifest)
    SyntheticConnector(tmp_path).ingest(store)
    jordan = next(p for p in store.people() if p.display_name == "Jordan Lee")
    assert jordan.message_count == manifest["stats"]["dm_messages"]
    assert jordan.handles == ("jordan.lee@example.com",)


def test_cli_generate_connect_people(tmp_path, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path / "home"))
    out = tmp_path / "hist"
    result = runner.invoke(
        app,
        ["synth", "generate", "bean-goose", "--seed", "4", "--messages", "1500", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "bean-goose-s4" in result.output
    manifest = json.loads((out / "manifest.json").read_text())
    monkeypatch.setenv(ENV_DIR, str(out))
    result = runner.invoke(app, ["connect", "synthetic"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["people"])
    line = next(ln for ln in result.output.splitlines() if "Alex Chen" in ln)
    assert int(line.split()[1]) == manifest["stats"]["dm_messages"]


def test_cli_unknown_scenario():
    result = runner.invoke(app, ["synth", "generate", "nope"])
    assert result.exit_code == 1


def test_cli_list():
    result = runner.invoke(app, ["synth", "list"])
    assert "bean-goose" in result.output and "noodle-summit" in result.output


def test_reactions_link_to_targets_with_kind(tmp_path):
    from sincehello.connectors.synthetic import SyntheticConnector
    from sincehello.store import Store

    with Store(tmp_path / "s.db") as store:
        SyntheticConnector().ingest(store)
        linked = store.conn.execute(
            "SELECT count(*), count(reacts_to_source_msg_id), count(reaction_kind)"
            " FROM message WHERE is_reaction = 1"
        ).fetchone()
        assert linked[0] > 0 and linked[0] == linked[1] == linked[2]
        kinds = {r[0] for r in store.conn.execute("SELECT DISTINCT reaction_kind FROM message")}
        assert "other" not in kinds and "love" in kinds
        orphans = store.conn.execute(
            "SELECT count(*) FROM message r WHERE r.is_reaction = 1 AND NOT EXISTS"
            " (SELECT 1 FROM message t WHERE t.source = r.source"
            "  AND t.source_msg_id = r.reacts_to_source_msg_id)"
        ).fetchone()[0]
        assert orphans == 0


def test_timezones_loaded_once(tmp_path):
    from sincehello.connectors.synthetic import SyntheticConnector
    from sincehello.store import Store

    with Store(tmp_path / "s.db") as store:
        SyntheticConnector().ingest(store)
        SyntheticConnector().ingest(store)
        alex = store.person_for_handle("synthetic", "+12125550142")
        tzs = [p["iana_tz"] for p in store.tz_periods(alex)]
        assert tzs == ["America/New_York", "Asia/Tokyo", "America/New_York"]
        assert [p["iana_tz"] for p in store.tz_periods(1)] == ["America/New_York"]


def test_timezones_can_be_withheld(tmp_path):
    from sincehello.connectors.synthetic import SyntheticConnector
    from sincehello.store import Store

    with Store(tmp_path / "s.db") as store:
        SyntheticConnector(load_timezones=False).ingest(store)
        assert store.conn.execute("SELECT count(*) FROM tz_period").fetchone()[0] == 0
