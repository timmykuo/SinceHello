"""Per-person timezone resolver (SPEC.md §6.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sincehello.store import Store

# When periods overlap, the more trusted source wins, then the later start.
_SOURCE_PRIORITY = {"user": 2, "inferred_confirmed": 1, "default": 0}


class TzResolver:
    """Maps (person_id, utc_ms) to the zone that person was in at that moment."""

    def __init__(self, store: Store, default_tz: str) -> None:
        self.store = store
        self.default_tz = default_tz
        self._default = ZoneInfo(default_tz)
        self._periods: dict[int, list[tuple[int, int | None, ZoneInfo]]] = {}

    def has_periods(self, person_id: int) -> bool:
        return bool(self._load(person_id))

    def zone(self, person_id: int, utc_ms: int) -> ZoneInfo:
        for start, end, tz in self._load(person_id):
            if start <= utc_ms and (end is None or utc_ms < end):
                return tz
        return self._default

    def local(self, person_id: int, utc_ms: int) -> datetime:
        return datetime.fromtimestamp(utc_ms / 1000, UTC).astimezone(self.zone(person_id, utc_ms))

    def _load(self, person_id: int) -> list[tuple[int, int | None, ZoneInfo]]:
        if person_id not in self._periods:
            rows = sorted(
                self.store.tz_periods(person_id),
                key=lambda r: (_SOURCE_PRIORITY.get(r["source"], 0), r["start_utc"], r["id"]),
                reverse=True,
            )
            self._periods[person_id] = [
                (r["start_utc"], r["end_utc"], ZoneInfo(r["iana_tz"])) for r in rows
            ]
        return self._periods[person_id]
