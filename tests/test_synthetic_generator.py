"""The offline generator and its manifest. Fact checks recompute from messages.jsonl
with plain code, independent of evals.generator.manifest."""

from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from evals import generator
from evals.generator import manifest as manifest_mod
from evals.generator import scenario as scenario_mod
from evals.generator import text

SAMPLE = Path(__file__).resolve().parents[1] / "evals" / "samples" / "bean-goose"


def ts(r: dict) -> datetime:
    return datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def sample():
    records, manifest = generator.read(SAMPLE)
    facts = {f["id"]: f for f in manifest["facts"]}
    dm = [r for r in records if r["conv"] == "dm" and not r["is_reaction"]]
    return records, manifest, facts, dm


def first_match(rows: list[dict], pattern: str) -> dict:
    rx = re.compile(pattern, re.IGNORECASE)
    return next(r for r in rows if r["text"] and rx.search(r["text"]))


def count_matches(rows: list[dict], pattern: str) -> Counter:
    rx = re.compile(pattern, re.IGNORECASE)
    return Counter(r["sender"] for r in rows if r["text"] and rx.search(r["text"]))


# --- scenarios and determinism -----------------------------------------------------


@pytest.mark.parametrize("name", scenario_mod.available())
def test_scenarios_load_and_corpus_has_no_reserved_terms(name):
    sc = scenario_mod.load(name)
    text.check_corpus(sc.reserved_patterns())
    types = {f["type"] for f in sc.facts}
    assert {
        "first_message",
        "first_phrase",
        "pet_name",
        "inside_joke",
        "busiest_day",
        "conflict_makeup",
        "reply_speed",
    } <= types


def test_some_scenario_has_a_long_distance_era():
    sc = scenario_mod.load("bean-goose")
    assert any(f["type"] == "long_distance" for f in sc.facts)


def test_same_seed_same_history():
    a = generator.generate("noodle-summit", seed=5, target_messages=1500)
    b = generator.generate("noodle-summit", seed=5, target_messages=1500)
    c = generator.generate("noodle-summit", seed=6, target_messages=1500)
    assert a == b
    assert a[0] != c[0]


def test_committed_sample_is_reproducible(sample):
    records, manifest, _, _ = sample
    regen, regen_manifest = generator.generate(
        manifest["scenario_id"], manifest["seed"], manifest["generator"]["target_messages"]
    )
    assert regen == records
    assert regen_manifest == manifest


def test_sample_is_small(sample):
    records, *_ = sample
    assert 2500 <= len(records) <= 3500
    size = sum(p.stat().st_size for p in SAMPLE.iterdir())
    assert size < 2 * 1024 * 1024


@pytest.mark.parametrize("name", ["bean-goose", "noodle-summit"])
@pytest.mark.parametrize("target", [5000, 50000])
def test_volume_is_configurable(name, target):
    records, manifest = generator.generate(name, seed=1, target_messages=target)
    assert abs(len(records) - target) / target < 0.12
    assert manifest["stats"]["lines"] == len(records)


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("name", ["bean-goose", "noodle-summit"])
def test_generation_validates_across_seeds(name, seed):
    records, manifest = generator.generate(name, seed=seed, target_messages=2000)
    manifest_mod.validate(scenario_mod.load(name), records, manifest)


def test_validate_catches_a_broken_fact(sample):
    records, manifest, facts, _ = sample
    sc = scenario_mod.load("bean-goose")
    broken = [dict(r) for r in records]
    early = next(
        r
        for r in broken
        if r["conv"] == "dm"
        and r["fact"] is None
        and r["text"]
        and ts(r) < ts({"ts": facts["first_ily"]["sent_at_utc"]})
    )
    early["text"] = "ok i love you"
    with pytest.raises(ValueError, match="first_ily"):
        manifest_mod.validate(sc, broken, manifest_mod.compute(sc, broken, 42, {}))


# --- format --------------------------------------------------------------------------


def test_record_format(sample):
    records, manifest, _, _ = sample
    keys = {
        "id",
        "conv",
        "ts",
        "sender",
        "text",
        "is_reaction",
        "reacts_to",
        "has_attachment",
        "edited",
        "fact",
        "role",
    }
    senders = set(manifest["participants"])
    times = [ts(r) for r in records]
    assert all(a < b for a, b in zip(times, times[1:], strict=False))
    for r in records:
        assert set(r) == keys
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r["ts"])
        assert r["id"].startswith(manifest["history_id"] + "-")
        assert r["conv"] in {"dm", "group"} and r["sender"] in senders
        assert r["text"] is not None or r["has_attachment"]
        if r["conv"] == "dm":
            assert r["sender"] in {"me", "them"}
    ids = {r["id"] for r in records}
    assert all(r["reacts_to"] in ids for r in records if r["is_reaction"])


def test_sample_exercises_groups_reactions_and_flags(sample):
    records, manifest, _, _ = sample
    s = manifest["stats"]
    assert s["group_messages"] > 50 and s["dm_reactions"] > 50
    assert s["attachment_only"] > 0 and s["edited"] > 0
    assert s["lines"] == s["dm_messages"] + s["dm_reactions"] + s["group_messages"]
    reaction = next(r for r in records if r["is_reaction"] and r["fact"] is None)
    assert re.match(r"^(Loved|Liked|Laughed at|Emphasized) \u201c", reaction["text"])


def test_filler_never_contains_reserved_terms(sample):
    records, *_ = sample
    patterns = scenario_mod.load("bean-goose").reserved_patterns()
    for r in records:
        if r["fact"] is None and r["text"] and not r["is_reaction"] and r["conv"] == "dm":
            assert not any(p.search(r["text"]) for p in patterns), r


# --- planted facts, recomputed independently ------------------------------------------


def test_first_message(sample):
    records, manifest, facts, dm = sample
    f = facts["first_message"]
    assert records[0]["id"] == dm[0]["id"] == f["message_id"]
    assert f["sender"] == "them" and f["text"] == dm[0]["text"]


def test_first_i_love_you(sample):
    records, _, facts, dm = sample
    f = facts["first_ily"]
    first = first_match(dm, f["pattern"])
    assert (first["id"], first["sender"], first["ts"]) == (
        f["message_id"],
        f["sender"],
        f["sent_at_utc"],
    )
    assert f["sender"] == "them" and f["sender_local_time"] == "2021-05-14T23:48:00"
    reply = next(r for r in dm if ts(r) > ts(first) and r["sender"] != first["sender"])
    assert reply["id"] == f["reply_message_id"]
    assert (ts(reply) - ts(first)).total_seconds() == f["reply_delay_s"] == 41
    assert count_matches(dm, f["pattern"]) == Counter(
        {k: v for k, v in f["count_by_sender"].items() if v}
    )
    # Traps: an earlier group-chat match, a 'love your' decoy, reactions quoting it.
    group = first_match([r for r in records if r["conv"] == "group"], f["pattern"])
    assert ts(group) < ts(first) and group["id"] == f["traps"]["earliest_group_match_id"]
    decoy = next(r for r in dm if r["id"] in f["decoy_message_ids"])
    assert "love your" in decoy["text"] and ts(decoy) < ts(first)
    assert f["traps"]["reaction_match_ids"]


def test_pet_name(sample):
    _, _, facts, dm = sample
    f = facts["pet_bean"]
    first = first_match(dm, f["pattern"])
    assert first["id"] == f["first_message_id"] and first["sender"] == f["first_sender"] == "me"
    local = ts(first).astimezone(ZoneInfo("America/New_York")).date()
    assert local.isoformat() >= "2021-05-12" and local.isoformat() < "2021-06-12"  # month 4
    assert f["relationship_month"] == 4
    counts = count_matches(dm, f["pattern"])
    assert dict(counts) == f["count_by_sender"]
    assert counts["me"] > counts["them"] > 0


def test_inside_joke(sample):
    _, _, facts, dm = sample
    f = facts["goose_incident"]
    origin = first_match(dm, f["pattern"])
    assert origin["id"] == f["origin_message_id"] and origin["sender"] == "them"
    assert origin["ts"].startswith("2021-04-03")
    assert sum(count_matches(dm, f["pattern"]).values()) == f["count"] >= 30
    assert origin["id"] in f["origin_context_ids"]


def test_busiest_day(sample):
    _, _, facts, dm = sample
    f = facts["moving_day"]
    ny = ZoneInfo("America/New_York")
    per_day = Counter(ts(r).astimezone(ny).date().isoformat() for r in dm)
    (day, n), (_, runner_up) = per_day.most_common(2)
    assert (day, n) == (f["date"], f["count"]) == ("2021-11-06", f["count"])
    assert runner_up == f["runner_up"]["count"] < n
    utc_day, _ = Counter(ts(r).date().isoformat() for r in dm).most_common(1)[0]
    assert utc_day == f["date"]


def test_conflict_is_the_longest_silence(sample):
    _, _, facts, dm = sample
    f = facts["the_fight"]
    gaps = [(ts(b) - ts(a), a, b) for a, b in zip(dm, dm[1:], strict=False)]
    gap, before, after = max(gaps, key=lambda g: g[0])
    assert (before["id"], after["id"]) == (f["last_before_silence_id"], f["apology_message_id"])
    assert gap == timedelta(hours=60) and f["silence_hours"] == 60
    assert after["sender"] == f["apology_sender"] == "me" and "sorry" in after["text"]


def test_long_distance_era(sample):
    records, manifest, facts, dm = sample
    f = facts["tokyo"]
    assert (f["tz"], f["offset_diff_hours"], f["weeks"]) == ("Asia/Tokyo", 14.0, 12.0)
    periods = manifest["timezones"]["them"]
    assert periods[1] == {"start_utc": f["start_utc"], "end_utc": f["end_utc"], "tz": f["tz"]}
    by_id = {r["id"]: r for r in records}
    assert "tokyo" in by_id[f["landed_message_id"]]["text"]
    assert f["start_utc"] < by_id[f["landed_message_id"]]["ts"] < f["end_utc"]
    assert by_id[f["reunion_message_id"]]["ts"] > f["end_utc"]
    # Alex sleeps on Tokyo time during the era: almost nothing sent 03:00-08:59 local.
    start, end = (
        datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (f["start_utc"], f["end_utc"])
    )
    theirs = [r for r in dm if r["sender"] == "them" and start <= ts(r) < end]
    tokyo_night = [r for r in theirs if 3 <= ts(r).astimezone(ZoneInfo("Asia/Tokyo")).hour < 9]
    ny_night = [r for r in theirs if 3 <= ts(r).astimezone(ZoneInfo("America/New_York")).hour < 9]
    assert len(tokyo_night) <= 0.02 * len(theirs)
    assert len(ny_night) > 0.08 * len(theirs)  # their evening, my early morning


def test_everyone_sleeps_in_their_own_timezone(sample):
    _, _, _, dm = sample
    ny = ZoneInfo("America/New_York")
    mine = [r for r in dm if r["sender"] == "me"]
    asleep = [r for r in mine if 1 <= ts(r).astimezone(ny).hour < 7]
    assert len(asleep) <= 0.02 * len(mine)


def test_reply_speed_and_who_texts_first(sample):
    _, _, facts, dm = sample
    delays: dict[str, list[float]] = {"me": [], "them": []}
    starts = Counter({dm[0]["sender"]: 1})
    for a, b in zip(dm, dm[1:], strict=False):
        gap = (ts(b) - ts(a)).total_seconds()
        if a["sender"] != b["sender"] and gap <= 6 * 3600:
            delays[b["sender"]].append(gap)
        if gap >= 6 * 3600:
            starts[b["sender"]] += 1
    medians = {k: statistics.median(v) for k, v in delays.items()}
    f = facts["reply_speed"]
    assert medians == f["median_reply_s"] and f["faster"] == "them"
    assert medians["me"] > 3 * medians["them"]
    t = facts["texts_first"]
    assert dict(starts) == t["starts"] and t["sender"] == "them"
    assert starts["them"] > starts["me"]


def test_eras_cover_the_history(sample):
    _, manifest, _, dm = sample
    eras = manifest["eras"]
    assert [e["end_utc"] for e in eras[:-1]] == [e["start_utc"] for e in eras[1:]]
    assert sum(e["dm_messages"] for e in eras) == len(dm)
