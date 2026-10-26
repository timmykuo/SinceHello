"""Era detection (SPEC.md §7.2) on hand-written fixtures and the committed bean-goose sample."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ScriptedConnector

from sincehello.analysis.eras import Era, _segment, detect_eras, sample_era
from sincehello.connectors import ConversationRecord, HandleRecord, MessageRecord
from sincehello.connectors.synthetic import SAMPLE_DIR, SyntheticConnector
from sincehello.store import Store

TZ = "America/New_York"
# A detected boundary counts as finding a planted one within this many days. Boundaries
# fall on week starts, and the minimum era length (6 weeks) keeps the last bean-goose era
# (5 weeks) from being found exactly, so the tolerance is two weeks.
TOLERANCE_DAYS = 14


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text())
    with Store(tmp_path_factory.mktemp("sample") / "s.db") as store:
        SyntheticConnector(SAMPLE_DIR).ingest(store)
        person = next(p.person_id for p in store.people() if p.display_name == "Alex Chen")
        yield store, person, manifest


@pytest.fixture(scope="module")
def sample_eras(sample):
    store, person, _ = sample
    return detect_eras(store, person, default_tz=TZ)


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _days(a: datetime, b: datetime) -> float:
    return abs((a - b).total_seconds()) / 86400


def test_eras_cover_the_history(sample, sample_eras):
    store, _, manifest = sample
    stats = manifest["stats"]
    assert [e.id for e in sample_eras] == [f"era_{i}" for i in range(1, len(sample_eras) + 1)]
    assert sample_eras[0].start_utc == _utc(stats["range"]["first_utc"])
    assert sample_eras[-1].end_utc == _utc(stats["range"]["last_utc"]) + timedelta(milliseconds=1)
    for a, b in zip(sample_eras, sample_eras[1:], strict=False):
        assert a.end_utc == b.start_utc
        assert b.start_utc.weekday() == 0 and b.start_utc.tzinfo is not None
    assert sum(e.message_count for e in sample_eras) == stats["dm_messages"]
    assert 1 <= len(sample_eras) <= 8
    json.dumps([e.to_dict() for e in sample_eras])  # serializable


def test_boundaries_match_planted_eras(sample, sample_eras):
    """Recall and precision of era boundaries against the manifest, within the tolerance."""
    _, _, manifest = sample
    planted = [_utc(e["start_utc"]) for e in manifest["eras"][1:]]
    found = [e.start_utc for e in sample_eras[1:]]
    recalled = [p for p in planted if min(_days(p, f) for f in found) <= TOLERANCE_DAYS]
    precise = [f for f in found if min(_days(p, f) for p in planted) <= TOLERANCE_DAYS]
    assert len(recalled) == len(planted)  # recall 5/5
    assert len(precise) == len(found)  # precision: no spurious boundaries


def test_tokyo_stretch_is_its_own_era(sample, sample_eras):
    _, _, manifest = sample
    tokyo = next(f for f in manifest["facts"] if f["type"] == "long_distance")
    start, end = _utc(tokyo["start_utc"]), _utc(tokyo["end_utc"])
    era = next(e for e in sample_eras if e.signals["tz_gap_hours"] > 0)
    assert _days(era.start_utc, start) <= 7
    assert _days(era.end_utc, end) <= TOLERANCE_DAYS
    assert era.signals["tz_gap_hours"] == tokyo["offset_diff_hours"]
    assert sum(e.signals["tz_gap_hours"] > 0 for e in sample_eras) == 1


def test_era_signals(sample_eras):
    for e in sample_eras:
        assert {
            "weeks",
            "messages_per_week",
            "volume_vs_overall",
            "share_me",
            "late_night_share",
            "affection_share",
            "tz_gap_hours",
            "reply_median_s",
        } <= set(e.signals)
        assert all(isinstance(v, float | int) for v in e.signals.values())
    assert "vocab_shift" not in sample_eras[0].signals
    assert all("vocab_shift" in e.signals for e in sample_eras[1:])


def test_deterministic(sample, sample_eras):
    store, person, _ = sample
    assert detect_eras(store, person, default_tz=TZ) == sample_eras


def test_min_weeks_and_max_eras(sample):
    store, person, _ = sample
    assert len(detect_eras(store, person, max_eras=1, default_tz=TZ)) == 1
    assert len(detect_eras(store, person, max_eras=3, default_tz=TZ)) <= 3
    long = detect_eras(store, person, min_weeks=20, default_tz=TZ)
    assert all(e.signals["weeks"] >= 19 for e in long)
    with pytest.raises(ValueError):
        detect_eras(store, person, min_weeks=0)


def test_segment_finds_steps():
    rows = [[0.0, 0.0]] * 10 + [[5.0, 0.0]] * 8 + [[5.0, -4.0]] * 12
    assert _segment(rows, 3, 8, penalty=5.0) == [10, 18]
    assert _segment(rows, 3, 2, penalty=5.0) in ([10], [18])
    assert _segment(rows, 11, 8, penalty=5.0) == [11]  # min size pushes the cut
    assert _segment(rows, 3, 8, penalty=1e6) == []
    assert _segment(rows[:5], 3, 8, penalty=0.0) == []  # too short for two eras


# --- short histories ---------------------------------------------------------

ALEX = "+15550001111"
HANDLES = [HandleRecord("fake", ALEX, "Alex"), HandleRecord("fake", "+15550002222", "Jo")]
CONVS = [
    ConversationRecord("fake", "c-alex", False, (ALEX,)),
    ConversationRecord("fake", "c-group", True, (ALEX, "+15550002222")),
]


def _load(store, messages) -> int:
    ScriptedConnector(messages, HANDLES, CONVS).ingest(store)
    return next(p.person_id for p in store.people() if p.display_name == "Alex")


def _msg(i: int, when: datetime, me: bool, text: str, conv: str = "c-alex", **kw):
    return MessageRecord(
        source="fake",
        source_msg_id=str(i),
        source_conv_id=conv,
        is_from_me=me,
        sent_at_utc=when,
        text=text,
        sender=None if me else ALEX,
        **kw,
    )


def test_short_history_is_one_era(store):
    t0 = datetime(2023, 5, 3, 12, tzinfo=UTC)
    person = _load(
        store,
        [_msg(i, t0 + timedelta(days=i), i % 2 == 0, f"hello {i}") for i in range(30)],
    )
    eras = detect_eras(store, person, default_tz="UTC")
    assert len(eras) == 1
    assert eras[0].message_count == 30
    assert eras[0].start_utc == t0


def test_no_messages(store):
    person = _load(store, [_msg(1, datetime(2023, 1, 1, tzinfo=UTC), False, "hi", "c-group")])
    assert detect_eras(store, person, default_tz="UTC") == []


# --- sample_era ----------------------------------------------------------------


def _check_rows(rows, era: Era, n: int):
    assert 0 < len(rows) <= n
    times = [r["sent_at_utc"] for r in rows]
    assert times == sorted(times)
    lo, hi = era.start_utc.timestamp() * 1000, era.end_utc.timestamp() * 1000
    assert all(lo <= t < hi for t in times)
    assert all(r["is_reaction"] == 0 for r in rows)
    assert {r["sender"] for r in rows} <= {"me", "them"}
    assert len({r["id"] for r in rows}) == len(rows)


def test_sample_era_random(sample, sample_eras):
    store, person, _ = sample
    era = sample_eras[1]
    rows = sample_era(store, person, era, n=40, seed=3, default_tz=TZ)
    _check_rows(rows, era, 40)
    assert len(rows) == 40
    again = sample_era(store, person, era, n=40, seed=3, default_tz=TZ)
    assert [r["id"] for r in rows] == [r["id"] for r in again]
    other = sample_era(store, person, era, n=40, seed=4, default_tz=TZ)
    assert [r["id"] for r in rows] != [r["id"] for r in other]
    # 1:1 only: nothing from the group chat.
    group = {
        r[0]
        for r in store.conn.execute(
            "SELECT m.id FROM message m JOIN conversation c ON c.id = m.conversation_id"
            " WHERE c.is_group = 1"
        )
    }
    assert not group & {r["id"] for r in rows}


def test_sample_era_spike_days_finds_moving_day(sample, sample_eras):
    store, person, manifest = sample
    moving = next(f for f in manifest["facts"] if f["type"] == "busiest_day")
    day = _utc(moving["date"] + "T12:00:00Z")
    era = next(e for e in sample_eras if e.start_utc <= day < e.end_utc)
    rows = sample_era(store, person, era, n=30, strategy="spike_days", default_tz=TZ)
    _check_rows(rows, era, 30)
    on_day = [r for r in rows if _local_date(r["sent_at_utc"]) == moving["date"]]
    assert len(on_day) == 10  # n // 3 from the busiest day, which comes first


def _local_date(ms: int) -> str:
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(ms / 1000, ZoneInfo(TZ)).date().isoformat()


def test_sample_era_longest_threads(sample, sample_eras):
    store, person, _ = sample
    era = sample_eras[2]
    rows = sample_era(store, person, era, n=12, strategy="longest_threads", default_tz=TZ)
    _check_rows(rows, era, 12)
    assert len(rows) == 12
    # Chunks of consecutive messages: at most 3 runs of 4.
    everything = sample_era(store, person, era, n=era.message_count, default_tz=TZ)
    position = {r["id"]: i for i, r in enumerate(everything)}
    pos = [position[r["id"]] for r in rows]
    assert 1 + sum(1 for a, b in zip(pos, pos[1:], strict=False) if b != a + 1) <= 3


def test_sample_era_bad_args(sample, sample_eras):
    store, person, _ = sample
    with pytest.raises(ValueError):
        sample_era(store, person, sample_eras[0], strategy="nope")  # type: ignore[arg-type]
    assert sample_era(store, person, sample_eras[0], n=0) == []
