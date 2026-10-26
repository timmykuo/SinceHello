"""A tiny recall score for the discovery agent on a synthetic history (SPEC.md §10.1).

A planted fact counts as found when some insight cites one of its planted message ids.
Facts without a message id use a simple stand-in: the busiest day matches evidence sent
on that date, and the reply-speed / texts-first facts match a metric citing that stat.
`exact` means the insight cited the fact's key message (e.g. the "i love you" itself,
not just its reply). The full eval harness (exactness of values, hallucinations, judge)
is M6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

SOURCE_PREFIX = "synthetic:"

# fact type -> (key id field, other accepted id fields)
ID_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "first_message": ("message_id", ()),
    "first_phrase": ("message_id", ("reply_message_id",)),
    "pet_name": ("first_message_id", ()),
    "inside_joke": ("origin_message_id", ("origin_context_ids",)),
    "conflict_makeup": (
        "apology_message_id",
        ("fight_start_message_id", "last_before_silence_id"),
    ),
    "long_distance": ("landed_message_id", ("reunion_message_id",)),
}
STAT_FACTS = {"reply_speed": "q_reply_times_", "texts_first": "q_who_texts_first_"}


def _ids(fact: dict[str, Any], fields: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for f in fields:
        v = fact.get(f)
        if isinstance(v, str):
            out.add(v)
        elif isinstance(v, list):
            out.update(x for x in v if isinstance(x, str))
    return out


def score(
    manifest: dict[str, Any], records: list[dict[str, Any]], insights: list[dict[str, Any]]
) -> dict[str, Any]:
    ts = {r["id"]: r["ts"] for r in records}
    cited: dict[str, list[str]] = {}  # planted id -> insight ids citing it
    for ins in insights:
        for ev in ins.get("evidence", []):
            mid = ev["message_id"].removeprefix(SOURCE_PREFIX)
            cited.setdefault(mid, []).append(ins["id"])

    facts = []
    for fact in manifest["facts"]:
        hit: list[str] = []
        exact = False
        if fact["type"] in ID_FIELDS:
            key, others = ID_FIELDS[fact["type"]]
            key_ids = _ids(fact, (key,))
            for mid in key_ids | _ids(fact, others):
                hit += cited.get(mid, [])
            exact = any(mid in cited for mid in key_ids)
        elif fact["type"] == "busiest_day":
            tz = ZoneInfo(fact["tz"])
            for mid, ins_ids in cited.items():
                if mid in ts:
                    local = datetime.fromisoformat(ts[mid].replace("Z", "+00:00")).astimezone(tz)
                    if local.date().isoformat() == fact["date"]:
                        hit += ins_ids
            exact = bool(hit)
        elif fact["type"] in STAT_FACTS:
            prefix = STAT_FACTS[fact["type"]]
            hit = [
                i["id"]
                for i in insights
                if (i.get("metric") or {}).get("query_id", "").startswith(prefix)
            ]
            exact = bool(hit)
        facts.append(
            {
                "id": fact["id"],
                "type": fact["type"],
                "found": bool(hit),
                "exact": exact,
                "insights": sorted(set(hit)),
            }
        )
    found = sum(f["found"] for f in facts)
    return {
        "facts": facts,
        "found": found,
        "total": len(facts),
        "recall": round(found / len(facts), 4) if facts else 0.0,
        "exact": sum(f["exact"] for f in facts),
        "insights": len(insights),
    }
