"""Per-person timezone resolver (SPEC.md §6.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sincehello.store import Store

# When periods overlap, the more trusted source wins, then the later start. "inferred" is
# a pending high-confidence suggestion (tz_suggestion), used until the user answers it.
_SOURCE_PRIORITY = {"user": 3, "inferred_confirmed": 2, "inferred": 1, "default": 0}


class TzResolver:
    """Maps (person_id, utc_ms) to the zone that person was in at that moment.

    `use_inferred=False` ignores unconfirmed suggestions (timezone inference itself uses
    it to see only what the user has told it).
    """

    def __init__(self, store: Store, default_tz: str, *, use_inferred: bool = True) -> None:
        self.store = store
        self.default_tz = default_tz
        self.use_inferred = use_inferred
        self._default = ZoneInfo(default_tz)
        self._periods: dict[int, list[tuple[int, int | None, ZoneInfo, str]]] = {}

    def has_periods(self, person_id: int) -> bool:
        return bool(self._load(person_id))

    def zone(self, person_id: int, utc_ms: int) -> ZoneInfo:
        found = self._find(person_id, utc_ms)
        return found[2] if found else self._default

    def source(self, person_id: int, utc_ms: int) -> str:
        found = self._find(person_id, utc_ms)
        return found[3] if found else "default"

    def local(self, person_id: int, utc_ms: int) -> datetime:
        return datetime.fromtimestamp(utc_ms / 1000, UTC).astimezone(self.zone(person_id, utc_ms))

    def timeline(self, person_id: int) -> list[dict[str, Any]]:
        """Every period on record for a person, oldest first, with where it came from."""
        return [
            {"start_utc": _iso(s), "end_utc": _iso(e) if e else None, "tz": str(z), "source": src}
            for s, e, z, src in sorted(self._load(person_id), key=lambda p: p[0])
        ]

    def _find(self, person_id: int, utc_ms: int) -> tuple[int, int | None, ZoneInfo, str] | None:
        for p in self._load(person_id):
            if p[0] <= utc_ms and (p[1] is None or utc_ms < p[1]):
                return p
        return None

    def _load(self, person_id: int) -> list[tuple[int, int | None, ZoneInfo, str]]:
        if person_id not in self._periods:
            rows = [dict(r) for r in self.store.tz_periods(person_id)]
            if self.use_inferred:
                rows += [
                    {**dict(r), "source": "inferred"}
                    for r in self.store.tz_suggestions(person_id, status="pending")
                    if r["confidence"] == "high"
                ]
            rows.sort(
                key=lambda r: (_SOURCE_PRIORITY.get(r["source"], 0), r["start_utc"], r["id"]),
                reverse=True,
            )
            self._periods[person_id] = [
                (r["start_utc"], r["end_utc"], ZoneInfo(r["iana_tz"]), r["source"]) for r in rows
            ]
        return self._periods[person_id]


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
