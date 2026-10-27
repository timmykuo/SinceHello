"""Timezone inference (SPEC.md §6.1): detection, the confirm flow, recall on synthetic data."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeConnector, ScriptedConnector, default_messages
from typer.testing import CliRunner

from evals import generator, tz_recall
from sincehello import config
from sincehello.agent.context import ToolContext
from sincehello.analysis import tz_infer
from sincehello.analysis.tz import TzResolver
from sincehello.cli import app
from sincehello.connectors import ConversationRecord, HandleRecord, MessageRecord
from sincehello.connectors.synthetic import SAMPLE_DIR, SyntheticConnector
from sincehello.store import Store, identity

NY = "America/New_York"
ALEX = "+15550001111"
START = date(2022, 3, 1)
DAY_MS = 86_400_000


def rhythm(store: Store, days: int, them_zone) -> int:
    """`days` of texting: you in New York, them in `them_zone(day)`, both awake 10-23h."""
    msgs = []
    for d in range(days):
        day = START + timedelta(days=d)
        for who, zone, hours in (
            ("me", NY, (10, 13, 16, 19, 21, 23)),
            ("them", them_zone(d), (10, 13, 16, 19, 21, 23)),
        ):
            for h in hours:
                local = datetime.combine(day, time(h, 7), tzinfo=ZoneInfo(zone))
                msgs.append((local.astimezone(UTC), who))
    msgs.sort()
    records = [
        MessageRecord(
            source="fake",
            source_msg_id=str(i),
            source_conv_id="c-alex",
            is_from_me=who == "me",
            sent_at_utc=ts,
            text="landed!" if (who == "them" and i == 0) else "hey",
            sender=None if who == "me" else ALEX,
        )
        for i, (ts, who) in enumerate(msgs)
    ]
    ScriptedConnector(
        records,
        [HandleRecord("fake", ALEX, "Alex")],
        [ConversationRecord("fake", "c-alex", False, (ALEX,))],
    ).ingest(store)
    return store.person_for_handle("fake", ALEX)


def tokyo_between(lo: int, hi: int):
    return lambda d: "Asia/Tokyo" if lo <= d < hi else NY


def day_ms(d: int, zone: str = NY) -> int:
    local = datetime.combine(START + timedelta(days=d), time(0), tzinfo=ZoneInfo(zone))
    return int(local.timestamp() * 1000)


# --- detection ------------------------------------------------------------------------


def test_quiet_midpoint_is_the_middle_of_the_night():
    hist = [5] * 48
    for b in range(2, 16):  # silent 01:00-08:00
        hist[b] = 0
    assert tz_infer._quiet_midpoint(hist) == 4.5


def test_detects_a_long_stay(store):
    person = rhythm(store, 120, tokyo_between(40, 80))
    (sg,) = tz_infer.detect(store, person, default_tz=NY)
    assert (sg.person_id, sg.kind, sg.utc_offset_hours) == (person, "shift", 9.0)
    assert sg.iana_tz == "Etc/GMT-9"
    assert sg.confidence == "high"
    assert abs(sg.start_utc - day_ms(40)) < 2 * DAY_MS
    assert abs(sg.end_utc - day_ms(80)) < 2 * DAY_MS


def test_short_trips_are_ignored(store):
    person = rhythm(store, 120, tokyo_between(50, 55))
    assert tz_infer.detect(store, person, default_tz=NY) == []


def test_someone_who_lives_elsewhere_gets_a_home_period(store):
    person = rhythm(store, 90, lambda d: "Asia/Tokyo")
    (sg,) = tz_infer.detect(store, person, default_tz=NY)
    assert (sg.kind, sg.end_utc, sg.utc_offset_hours, sg.confidence) == ("home", None, 9.0, "high")


def test_a_known_period_is_not_suggested_again(store):
    person = rhythm(store, 120, tokyo_between(40, 80))
    start = datetime.fromtimestamp(day_ms(40) / 1000, UTC)
    end = datetime.fromtimestamp(day_ms(80) / 1000, UTC)
    store.add_tz_period(person, start, end, "Asia/Tokyo", "user")
    assert tz_infer.detect(store, person, default_tz=NY) == []


# --- storage and the confirm flow -----------------------------------------------------


def test_sync_is_idempotent(store):
    person = rhythm(store, 120, tokyo_between(40, 80))
    first = tz_infer.sync(store, person, default_tz=NY)
    again = tz_infer.sync(store, person, default_tz=NY)
    assert [s.id for s in first] == [s.id for s in again] and len(first) == 1
    assert len(store.tz_suggestions(person)) == 1


def test_ignored_suggestions_stay_ignored(store):
    person = rhythm(store, 120, tokyo_between(40, 80))
    (sg,) = tz_infer.sync(store, person, default_tz=NY)
    tz_infer.ignore(store, sg)
    assert tz_infer.sync(store, person, default_tz=NY) == []
    assert TzResolver(store, NY).source(person, day_ms(60)) == "default"


def test_accepting_writes_a_confirmed_period(store):
    person = rhythm(store, 120, tokyo_between(40, 80))
    (sg,) = tz_infer.sync(store, person, default_tz=NY)
    tz_infer.accept(store, sg)
    (period,) = store.tz_periods(person)
    assert (period["iana_tz"], period["source"]) == ("Etc/GMT-9", "inferred_confirmed")
    assert store.tz_suggestions(person)[0]["status"] == "confirmed"
    assert tz_infer.sync(store, person, default_tz=NY) == []


def test_editing_writes_a_user_period(store):
    person = rhythm(store, 120, tokyo_between(40, 80))
    (sg,) = tz_infer.sync(store, person, default_tz=NY)
    tz_infer.accept(store, sg, iana_tz="Asia/Tokyo", end_utc=day_ms(80, "Asia/Tokyo"))
    (period,) = store.tz_periods(person)
    assert (period["iana_tz"], period["source"]) == ("Asia/Tokyo", "user")
    assert period["end_utc"] == day_ms(80, "Asia/Tokyo")
    with pytest.raises(ValueError, match="end after it starts"):
        tz_infer.accept(store, sg, end_utc=sg.start_utc)


def test_only_high_confidence_suggestions_are_used_unconfirmed(store):
    person = rhythm(store, 120, tokyo_between(40, 80))
    (sg,) = tz_infer.sync(store, person, default_tz=NY)
    mid = day_ms(60)
    assert TzResolver(store, NY).source(person, mid) == "inferred"
    assert str(TzResolver(store, NY).zone(person, mid)) == "Etc/GMT-9"
    assert TzResolver(store, NY, use_inferred=False).source(person, mid) == "default"
    store.update_tz_suggestion(sg.id, confidence="medium")
    assert TzResolver(store, NY).source(person, mid) == "default"
    # Labeled in what the agent and report.json see.
    store.update_tz_suggestion(sg.id, confidence="high")
    timeline = ToolContext(store, person, NY).timezones()
    assert timeline["me"] == [] and timeline["them"][0]["source"] == "inferred"


def test_merging_people_moves_their_suggestions(store):
    FakeConnector(messages=default_messages()).ingest(store)
    sam = store.person_for_handle("fake", "+15550002222")
    alex = store.person_for_handle("fake", ALEX)
    store.add_tz_suggestion(
        sam,
        kind="home",
        start_utc=0,
        utc_offset_hours=9.0,
        iana_tz="Etc/GMT-9",
        confidence="low",
        score=0.1,
    )
    identity.merge_handle(store, alex, identity.find_handle(store, "+15550002222").id)
    assert len(store.tz_suggestions(alex)) == 1


# --- planted long-distance eras (SPEC §10.1) ------------------------------------------


def test_finds_the_planted_tokyo_era_in_the_committed_sample():
    r = tz_recall.run(SAMPLE_DIR)
    (tokyo,) = r["expected"]
    assert tokyo["found"] and tokyo["tz"] == "Asia/Tokyo"
    assert tokyo["offset"] == 9.0 and tokyo["confidence"] == "high"
    assert abs(tokyo["start_error_days"]) <= 1 and abs(tokyo["end_error_days"]) <= 7
    assert r["trap_hits"] == [] and r["false_positives"] == []
    (sg,) = r["suggestions"]
    assert sg["hint_quote"].startswith("just landed in tokyo")


@pytest.mark.parametrize(("seed", "lines"), [(3, 3000), (2, 6000)])
def test_finds_the_planted_san_francisco_era_and_skips_the_short_trip(tmp_path, seed, lines):
    records, manifest = generator.generate("noodle-summit", seed=seed, target_messages=lines)
    generator.write(tmp_path, records, manifest)
    r = tz_recall.run(tmp_path)
    (sf,) = r["expected"]
    assert sf["found"] and sf["tz"] == "America/Los_Angeles"
    assert sf["confidence"] in ("medium", "high")
    # Maya's 4-day Dubai trip is a trap: too short to suggest.
    (trap,) = tz_recall.planted(manifest)[1]
    assert trap["tz"] == "Asia/Dubai"
    assert r["trap_hits"] == [] and r["false_positives"] == []


def test_truth_loaded_by_the_connector_leaves_nothing_to_suggest(bean_goose):
    store, person = bean_goose
    assert tz_infer.detect(store, person, default_tz=NY) == []


# --- CLI ------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_store(tmp_path, monkeypatch):
    """The committed sample without its true timezones, in an isolated SINCEHELLO_HOME."""
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path))
    monkeypatch.setenv("SINCEHELLO_TZ", NY)
    with Store(config.store_path()) as s:
        SyntheticConnector(load_timezones=False).ingest(s)
        person = s.person_for_handle("synthetic", "+12125550142")
    return person


def test_cli_infer_then_confirm(cli_store):
    person = str(cli_store)
    result = runner.invoke(app, ["tz", "infer", person])
    assert result.exit_code == 0, result.output
    assert "Looks like Alex Chen was about 14h ahead of you from Jan 8, 2022" in result.output
    assert "just landed in tokyo" in result.output and "Confidence: high" in result.output

    result = runner.invoke(app, ["tz", "confirm", person], input="x\nc\n")
    assert result.exit_code == 0, result.output
    assert "Saved: Etc/GMT-9." in result.output
    result = runner.invoke(app, ["tz", "show", person])
    assert "Etc/GMT-9" in result.output and "inferred_confirmed" in result.output
    assert "waiting" not in result.output
    assert "Nothing to confirm." in runner.invoke(app, ["tz", "confirm", person]).output


def test_cli_edit(cli_store):
    person = str(cli_store)
    answers = "e\nAsia/Tokyo\n2022-01-09 01:00\n2022-04-03\n"
    result = runner.invoke(app, ["tz", "confirm", person], input=answers)
    assert result.exit_code == 0, result.output
    with Store(config.store_path()) as s:
        (p,) = s.tz_periods(cli_store)
    assert (p["iana_tz"], p["source"]) == ("Asia/Tokyo", "user")
    assert p["start_utc"] == int(datetime(2022, 1, 9, 6, tzinfo=UTC).timestamp() * 1000)


def test_cli_ignore_and_json(cli_store):
    person = str(cli_store)
    out = json.loads(runner.invoke(app, ["tz", "infer", person, "--json"]).output)
    assert [s["utc_offset_hours"] for s in out["suggestions"]] == [9.0]
    result = runner.invoke(app, ["tz", "confirm", person], input="i\n")
    assert "Ignored." in result.output
    assert "No timezone shifts to suggest." in runner.invoke(app, ["tz", "infer", person]).output


def test_cli_eval_tz():
    result = runner.invoke(app, ["eval", "tz"])
    assert result.exit_code == 0, result.output
    assert "bean-goose-s42: 1/1" in result.output and "recall 1/1 (100%)" in result.output
