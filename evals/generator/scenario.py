"""Scenario specs for synthetic histories (SPEC.md §10.1).

A scenario describes one fake relationship: two personas, when it starts and how long it
runs, per-person timezone periods (SPEC.md §6.1), eras, and the planted facts the eval
harness will look for. Scenarios live in ``evals/scenarios/<id>.json``.

Why JSON: it is in the standard library on both sides (Python here, the later TypeScript
UI), so the scenario, the generated ``manifest.json`` and the report contract all share
one format and no YAML dependency or YAML type-coercion surprises (``no`` -> False,
``2021-05-14`` -> date) creep into ground truth. Scenarios are small and hand-written, so
the lack of comments is handled with ``"description"`` / ``"note"`` fields instead.

Time conventions inside a scenario:

* ``start`` and era ``start`` are calendar dates in the ``me`` persona's local time.
* Timezone period ``start`` values are UTC instants (``2022-01-08T14:00:00Z``), matching
  ``tz_period.start_utc`` in the store.
* Planted-fact ``at`` values are naive local datetimes (``2021-05-14T23:48``) in the
  local time of the fact's first sender, or ``{"month": N, "day": D, "time": "HH:MM"}``
  meaning day D (1-based) of relationship month N.

See ``evals/README.md`` for the full field reference.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "scenarios"

FACT_TYPES = {
    "first_message": {"at", "script"},
    "first_phrase": {"phrase", "pattern", "at", "script"},
    "pet_name": {"term", "first", "rates", "forms"},
    "inside_joke": {"phrase", "pattern", "at", "script", "callbacks"},
    "busiest_day": {"date", "window", "factor", "min_messages", "script", "topic"},
    "conflict_makeup": {"at", "script", "silence_hours", "makeup"},
    "long_distance": {"person", "period", "landed", "reunion"},
    "reply_speed": {"faster"},
    "texts_first": {"sender", "gap_hours"},
}


@dataclass(frozen=True)
class Style:
    lowercase: float = 0.5  # chance a filler line is fully lowercased
    emoji_rate: float = 0.1  # chance an emoji is appended
    emojis: tuple[str, ...] = ("😂",)
    exclaim: float = 0.1  # chance trailing punctuation becomes "!!"
    abbrev: float = 0.2  # chance of texting abbreviations (you -> u, ...)
    double_text: float = 0.2  # chance a turn has more than one message


@dataclass(frozen=True)
class Persona:
    key: str  # "me" or "them"
    name: str
    handle: str | None  # fake phone/email; None for "me"
    style: Style
    sleep: tuple[time, time]  # local (start, end), may wrap midnight
    reply_median_s: float


@dataclass(frozen=True)
class TzPeriod:
    start_utc: datetime
    tz: str


@dataclass(frozen=True)
class Era:
    id: str
    name: str
    start: date
    volume: float  # relative messages-per-day weight
    topics: tuple[str, ...]


@dataclass(frozen=True)
class Friend:
    key: str
    name: str
    handle: str


@dataclass(frozen=True)
class GroupChat:
    id: str
    name: str
    friends: tuple[Friend, ...]
    share: float  # fraction of all lines that are group-chat messages
    script: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    start: date
    duration_days: int
    target_messages: int
    me: Persona
    them: Persona
    timezones: dict[str, tuple[TzPeriod, ...]]
    eras: tuple[Era, ...]
    initiator_share_them: float
    reaction_share: float
    attachment_share: float
    edited_share: float
    group: GroupChat
    reserved: tuple[str, ...]
    facts: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def personas(self) -> dict[str, Persona]:
        return {"me": self.me, "them": self.them}

    def fact(self, fact_id: str) -> dict[str, Any]:
        for f in self.facts:
            if f["id"] == fact_id:
                return f
        raise KeyError(fact_id)

    def reserved_patterns(self) -> list[re.Pattern[str]]:
        """Patterns filler text must never match, so planted facts stay exact."""
        pats = list(self.reserved)
        for f in self.facts:
            if "pattern" in f:
                pats.append(f["pattern"])
            if f["type"] == "pet_name":
                pats.append(rf"\b{re.escape(f['term'])}\b")
        return [re.compile(p, re.IGNORECASE) for p in pats]


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"UTC timestamp needs a 'Z' or offset: {s!r}")
    return dt.astimezone(UTC)


def _hhmm(s: str) -> time:
    return time.fromisoformat(s)


def _persona(key: str, raw: dict[str, Any]) -> Persona:
    style = raw.get("style", {})
    return Persona(
        key=key,
        name=raw["name"],
        handle=raw.get("handle"),
        style=Style(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in style.items()}),
        sleep=(_hhmm(raw["sleep"][0]), _hhmm(raw["sleep"][1])),
        reply_median_s=float(raw["reply_median_s"]),
    )


def from_dict(raw: dict[str, Any]) -> Scenario:
    personas = raw["personas"]
    if set(personas) != {"me", "them"}:
        raise ValueError("personas must be exactly 'me' and 'them'")
    if not personas["them"].get("handle"):
        raise ValueError("personas.them.handle is required")
    timezones = {
        who: tuple(TzPeriod(parse_utc(p["start"]), p["tz"]) for p in periods)
        for who, periods in raw["timezones"].items()
    }
    if set(timezones) != {"me", "them"}:
        raise ValueError("timezones must cover 'me' and 'them'")
    for who, periods in timezones.items():
        if any(a.start_utc >= b.start_utc for a, b in zip(periods, periods[1:], strict=False)):
            raise ValueError(f"timezones.{who} must be in increasing order")
    eras = tuple(
        Era(
            e["id"],
            e["name"],
            date.fromisoformat(e["start"]),
            float(e["volume"]),
            tuple(e["topics"]),
        )
        for e in raw["eras"]
    )
    start = date.fromisoformat(raw["start"])
    if not eras or eras[0].start != start:
        raise ValueError("the first era must start on the scenario start date")
    g = raw["group_chat"]
    group = GroupChat(
        id=g["id"],
        name=g["name"],
        friends=tuple(Friend(f["key"], f["name"], f["handle"]) for f in g["friends"]),
        share=float(g["share"]),
        script=tuple(g.get("script", [])),
    )
    facts = tuple(raw["facts"])
    seen: set[str] = set()
    for f in facts:
        missing = FACT_TYPES.get(f.get("type"), {"<unknown type>"}) - set(f)
        if missing or "id" not in f:
            raise ValueError(f"fact {f.get('id')!r} ({f.get('type')}): missing {missing}")
        if f["id"] in seen:
            raise ValueError(f"duplicate fact id {f['id']!r}")
        seen.add(f["id"])
    if sum(f["type"] == "first_message" for f in facts) != 1:
        raise ValueError("a scenario needs exactly one first_message fact")
    return Scenario(
        id=raw["id"],
        description=raw.get("description", ""),
        start=start,
        duration_days=int(raw["duration_days"]),
        target_messages=int(raw["target_messages"]),
        me=_persona("me", personas["me"]),
        them=_persona("them", personas["them"]),
        timezones=timezones,
        eras=eras,
        initiator_share_them=float(raw.get("initiator_share", {}).get("them", 0.5)),
        reaction_share=float(raw.get("reaction_share", 0.03)),
        attachment_share=float(raw.get("attachment_share", 0.01)),
        edited_share=float(raw.get("edited_share", 0.01)),
        group=group,
        reserved=tuple(raw.get("reserved", [])),
        facts=facts,
    )


def load(name_or_path: str | Path) -> Scenario:
    """Load a scenario by id (``bean-goose``) or by path to a JSON file."""
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIO_DIR / f"{name_or_path}.json"
    return from_dict(json.loads(path.read_text()))


def available() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))
