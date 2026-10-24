"""Ground truth for a generated history: compute ``manifest.json`` and check it.

Every expected value is computed from the final messages (never assumed from the
scenario), then ``validate`` checks the computed values against what the scenario meant
to plant. The definitions here are the contract other workstreams test against; they are
documented in ``evals/README.md``.

Shared definitions:

* "1:1 messages": ``conv == "dm"`` and ``is_reaction`` false. Attachment-only messages
  (``text`` null) count toward totals but never match text patterns.
* Text matching: case-insensitive ``re.search`` on ``text``.
* Local time: the sender's timezone period at that instant (``manifest.timezones``).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from evals.generator.clock import Clock
from evals.generator.scenario import Scenario, parse_utc

FORMAT_VERSION = 1
SOURCE = "synthetic"
SESSION_GAP = timedelta(hours=6)  # replies slower than this don't count as replies
LATE_NIGHT_HOURS = range(0, 4)  # 00:00-03:59 in the sender's local time


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Facts:
    """Read-only helpers over message records (the dicts in ``messages.jsonl``)."""

    def __init__(self, scenario: Scenario, records: list[dict[str, Any]]) -> None:
        self.sc = scenario
        self.clock = Clock(scenario)
        self.records = records
        self.ts = {r["id"]: parse_utc(r["ts"]) for r in records}
        self.dm = [r for r in records if r["conv"] == "dm" and not r["is_reaction"]]

    def t(self, r: dict[str, Any]) -> datetime:
        return self.ts[r["id"]]

    def local(self, r: dict[str, Any]) -> datetime:
        return self.clock.local(r["sender"], self.t(r))

    def matches(self, pattern: str, rows: list[dict[str, Any]] | None = None) -> list[dict]:
        rx = re.compile(pattern, re.IGNORECASE)
        return [
            r for r in (self.dm if rows is None else rows) if r["text"] and rx.search(r["text"])
        ]

    def planted(self, fact_id: str, role: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["fact"] == fact_id and r["role"] == role]

    def by_sender(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        c = Counter(r["sender"] for r in rows)
        return {"me": c.get("me", 0), "them": c.get("them", 0)}

    def gaps(self) -> list[tuple[dict, dict, timedelta]]:
        return [(a, b, self.t(b) - self.t(a)) for a, b in zip(self.dm, self.dm[1:], strict=False)]

    def reply_medians(self) -> dict[str, float]:
        delays: dict[str, list[float]] = {"me": [], "them": []}
        for a, b, gap in self.gaps():
            if a["sender"] != b["sender"] and gap <= SESSION_GAP:
                delays[b["sender"]].append(gap.total_seconds())
        return {k: statistics.median(v) for k, v in delays.items()}

    def conversation_starts(self, gap: timedelta) -> dict[str, int]:
        starts = [self.dm[0]] + [b for _, b, g in self.gaps() if g >= gap]
        return self.by_sender(starts)

    def daily_counts(self, who_tz: str | None) -> Counter[date]:
        """1:1 messages per calendar date, in ``who_tz``'s local time (None = UTC)."""
        if who_tz is None:
            return Counter(self.t(r).date() for r in self.dm)
        return Counter(self.clock.local(who_tz, self.t(r)).date() for r in self.dm)

    def longest_silence(self) -> tuple[dict, dict, timedelta]:
        return max(self.gaps(), key=lambda x: x[2])


def compute(
    scenario: Scenario, records: list[dict[str, Any]], seed: int, generator: dict[str, Any]
) -> dict[str, Any]:
    fx = Facts(scenario, records)
    history_id = f"{scenario.id}-s{seed}"
    participants: dict[str, Any] = {
        "me": {"name": scenario.me.name, "is_me": True},
        "them": {"name": scenario.them.name, "handle": scenario.them.handle},
    }
    for f in scenario.group.friends:
        participants[f.key] = {"name": f.name, "handle": f.handle}
    end = fx.clock.local_midnight_utc("me", scenario.start + timedelta(days=scenario.duration_days))
    timezones = {}
    for who, periods in scenario.timezones.items():
        timezones[who] = [
            {
                "start_utc": iso(p.start_utc),
                "end_utc": iso(periods[i + 1].start_utc) if i + 1 < len(periods) else None,
                "tz": p.tz,
            }
            for i, p in enumerate(periods)
        ]
    eras = []
    for i, e in enumerate(scenario.eras):
        start = fx.clock.local_midnight_utc("me", e.start)
        stop = (
            fx.clock.local_midnight_utc("me", scenario.eras[i + 1].start)
            if i + 1 < len(scenario.eras)
            else end
        )
        eras.append(
            {
                "id": e.id,
                "name": e.name,
                "start_utc": iso(start),
                "end_utc": iso(stop),
                "dm_messages": sum(start <= fx.t(r) < stop for r in fx.dm),
            }
        )
    a, b, gap = fx.longest_silence()
    late = {
        who: sum(fx.local(r).hour in LATE_NIGHT_HOURS for r in fx.dm if r["sender"] == who)
        / max(1, sum(r["sender"] == who for r in fx.dm))
        for who in ("me", "them")
    }
    stats = {
        "lines": len(records),
        "dm_messages": len(fx.dm),
        "dm_by_sender": fx.by_sender(fx.dm),
        "dm_reactions": sum(r["conv"] == "dm" and r["is_reaction"] for r in records),
        "group_messages": sum(r["conv"] == "group" and not r["is_reaction"] for r in records),
        "attachment_only": sum(r["has_attachment"] and r["text"] is None for r in records),
        "edited": sum(r["edited"] for r in records),
        "first_message_id": fx.dm[0]["id"],
        "last_message_id": fx.dm[-1]["id"],
        "range": {"first_utc": iso(fx.t(fx.dm[0])), "last_utc": iso(fx.t(fx.dm[-1]))},
        "longest_silence": {
            "hours": round(gap.total_seconds() / 3600, 3),
            "after_message_id": a["id"],
            "next_message_id": b["id"],
        },
        "reply_median_s": fx.reply_medians(),
        "late_night_share": {k: round(v, 4) for k, v in late.items()},
    }
    facts = []
    for f in scenario.facts:
        fn = globals().get(f"_fact_{f['type']}")
        entry = {"id": f["id"], "type": f["type"], **(fn(fx, f) if fn else {})}
        facts.append(entry)
    return {
        "format_version": FORMAT_VERSION,
        "history_id": history_id,
        "scenario_id": scenario.id,
        "seed": seed,
        "source": SOURCE,
        "generator": generator,
        "description": scenario.description,
        "participants": participants,
        "conversations": [
            {
                "key": "dm",
                "source_conv_id": f"{history_id}:dm",
                "is_group": False,
                "members": ["them"],
            },
            {
                "key": "group",
                "source_conv_id": f"{history_id}:{scenario.group.id}",
                "is_group": True,
                "name": scenario.group.name,
                "members": ["them", *(f.key for f in scenario.group.friends)],
            },
        ],
        "timezones": timezones,
        "eras": eras,
        "stats": stats,
        "facts": facts,
    }


def _one(rows: list[dict[str, Any]], what: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise ValueError(f"expected exactly one {what}, found {len(rows)}")
    return rows[0]


def _fact_first_message(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    r = fx.dm[0]
    return {"message_id": r["id"], "sent_at_utc": r["ts"], "sender": r["sender"], "text": r["text"]}


def _fact_first_phrase(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    hits = fx.matches(f["pattern"])
    first = hits[0]
    reply = next(r for r in fx.dm if fx.t(r) > fx.t(first) and r["sender"] != first["sender"])
    group_hits = fx.matches(f["pattern"], [r for r in fx.records if r["conv"] == "group"])
    reaction_hits = fx.matches(f["pattern"], [r for r in fx.records if r["is_reaction"]])
    return {
        "phrase": f["phrase"],
        "pattern": f["pattern"],
        "message_id": first["id"],
        "sent_at_utc": first["ts"],
        "sender": first["sender"],
        "sender_local_time": fx.local(first).strftime("%Y-%m-%dT%H:%M:%S"),
        "text": first["text"],
        "reply_message_id": reply["id"],
        "reply_sender": reply["sender"],
        "reply_delay_s": int((fx.t(reply) - fx.t(first)).total_seconds()),
        "count_by_sender": fx.by_sender(hits),
        "decoy_message_ids": [r["id"] for r in fx.planted(f["id"], "decoy")],
        "traps": {
            "earliest_group_match_id": group_hits[0]["id"] if group_hits else None,
            "reaction_match_ids": [r["id"] for r in reaction_hits][:20],
        },
    }


def _relationship_month(fx: Facts, r: dict[str, Any]) -> int:
    d = fx.clock.local("me", fx.t(r)).date()
    start = fx.sc.start
    months = (d.year - start.year) * 12 + d.month - start.month
    return months + (1 if d.day >= start.day else 0)


def _fact_pet_name(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    pattern = rf"\b{re.escape(f['term'])}\b"
    hits = fx.matches(pattern)
    first = hits[0]
    return {
        "term": f["term"],
        "pattern": pattern,
        "first_message_id": first["id"],
        "first_at_utc": first["ts"],
        "first_sender": first["sender"],
        "relationship_month": _relationship_month(fx, first),
        "count_by_sender": fx.by_sender(hits),
    }


def _fact_inside_joke(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    hits = fx.matches(f["pattern"])
    block = [
        r
        for r in fx.records
        if r["fact"] == f["id"] and r["role"] != "callback" and not r["is_reaction"]
    ]
    return {
        "phrase": f["phrase"],
        "pattern": f["pattern"],
        "origin_message_id": hits[0]["id"],
        "origin_at_utc": hits[0]["ts"],
        "origin_sender": hits[0]["sender"],
        "origin_context_ids": [r["id"] for r in block],
        "count": len(hits),
        "count_by_sender": fx.by_sender(hits),
    }


def _fact_busiest_day(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    local = fx.daily_counts("me").most_common()
    utc = fx.daily_counts(None).most_common()
    return {
        "date": local[0][0].isoformat(),
        "tz_of": "me",
        "tz": fx.clock.tz_at("me", fx.t(fx.dm[0])).key,
        "count": local[0][1],
        "runner_up": {"date": local[1][0].isoformat(), "count": local[1][1]},
        "utc_date": utc[0][0].isoformat(),
        "utc_count": utc[0][1],
        "reason": f.get("reason", ""),
    }


def _fact_conflict_makeup(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    start = _one(fx.planted(f["id"], "key"), "fight start")
    last = _one(fx.planted(f["id"], "last_before_silence"), "last message before silence")
    apology = _one(fx.planted(f["id"], "apology"), "apology")
    silence = fx.t(apology) - fx.t(last)
    a, b, longest = fx.longest_silence()
    return {
        "fight_start_message_id": start["id"],
        "fight_start_at_utc": start["ts"],
        "fight_starter": start["sender"],
        "last_before_silence_id": last["id"],
        "silence_start_utc": last["ts"],
        "silence_end_utc": apology["ts"],
        "silence_hours": silence.total_seconds() / 3600,
        "apology_message_id": apology["id"],
        "apology_sender": apology["sender"],
        "apology_text": apology["text"],
        "is_longest_silence": (a["id"], b["id"]) == (last["id"], apology["id"]),
    }


def _fact_long_distance(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    who = f["person"]
    periods = fx.sc.timezones[who]
    i = int(f["period"])
    start, end = periods[i].start_utc, periods[i + 1].start_utc
    diff = fx.clock.offset_hours(who, start) - fx.clock.offset_hours(other_key(who), start)
    return {
        "person": who,
        "tz": periods[i].tz,
        "other_tz": fx.clock.tz_at(other_key(who), start).key,
        "start_utc": iso(start),
        "end_utc": iso(end),
        "weeks": round((end - start).days / 7, 1),
        "offset_diff_hours": diff,
        "landed_message_id": _one(fx.planted(f["id"], "landed"), "landed")["id"],
        "reunion_message_id": _one(fx.planted(f["id"], "reunion"), "reunion")["id"],
        "dm_messages_during": sum(start <= fx.t(r) < end for r in fx.dm),
    }


def other_key(who: str) -> str:
    return "them" if who == "me" else "me"


def _fact_reply_speed(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    med = fx.reply_medians()
    return {
        "faster": min(med, key=med.get),
        "median_reply_s": med,
        "max_gap_hours": SESSION_GAP.total_seconds() / 3600,
    }


def _fact_texts_first(fx: Facts, f: dict[str, Any]) -> dict[str, Any]:
    starts = fx.conversation_starts(timedelta(hours=f["gap_hours"]))
    total = sum(starts.values())
    return {
        "sender": max(starts, key=starts.get),
        "gap_hours": f["gap_hours"],
        "starts": starts,
        "share": {k: round(v / total, 4) for k, v in starts.items()},
    }


def validate(scenario: Scenario, records: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    """Raise ValueError if a computed fact does not match what the scenario planted."""
    fx = Facts(scenario, records)
    problems: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            problems.append(msg)

    ids = [r["id"] for r in records]
    check(len(set(ids)) == len(ids), "message ids are not unique")
    check(records[0]["fact"] == "first_message", "the first line is not the first message")
    times = [fx.t(r) for r in records]
    check(
        all(a < b for a, b in zip(times, times[1:], strict=False)),
        "timestamps not strictly increasing",
    )
    for f, got in zip(scenario.facts, manifest["facts"], strict=True):
        t, fid = f["type"], f["id"]
        if t in ("first_message", "first_phrase"):
            key = _one(fx.planted(fid, "key"), f"{fid} key message")
            check(got["message_id"] == key["id"], f"{fid}: first match is not the planted one")
            check(got["sender"] == f["script"][0][0], f"{fid}: wrong sender")
        if t == "first_phrase":
            for d in got["decoy_message_ids"]:
                check(d != got["message_id"], f"{fid}: decoy counted as first")
        if t == "pet_name":
            key = _one(fx.planted(fid, "key"), f"{fid} key message")
            check(got["first_message_id"] == key["id"], f"{fid}: first use is not planted")
            check(
                got["relationship_month"]
                == f["first"]["at"].get("month", got["relationship_month"]),
                f"{fid}: first use not in the planned month",
            )
        if t == "inside_joke":
            key = _one(fx.planted(fid, "key"), f"{fid} key message")
            check(got["origin_message_id"] == key["id"], f"{fid}: origin is not planted")
        if t == "busiest_day":
            check(got["date"] == f["date"], f"{fid}: busiest day is {got['date']}")
            check(got["utc_date"] == f["date"], f"{fid}: busiest UTC day is {got['utc_date']}")
            check(got["count"] > got["runner_up"]["count"], f"{fid}: no clear busiest day")
        if t == "conflict_makeup":
            check(got["is_longest_silence"], f"{fid}: the silence is not the longest")
            check(abs(got["silence_hours"] - f["silence_hours"]) < 1 / 60, f"{fid}: silence")
        if t == "reply_speed":
            check(got["faster"] == f["faster"], f"{fid}: {got['faster']} replies faster")
        if t == "texts_first":
            check(got["sender"] == f["sender"], f"{fid}: {got['sender']} texts first")
    if problems:
        raise ValueError(
            "generated history does not match its scenario:\n  " + "\n  ".join(problems)
        )
