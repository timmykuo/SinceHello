"""Per-person local time and sleep (SPEC.md §6.1): everyone sleeps in their own timezone."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from evals.generator.scenario import Scenario, TzPeriod


class Clock:
    def __init__(self, scenario: Scenario) -> None:
        self.periods: dict[str, tuple[TzPeriod, ...]] = dict(scenario.timezones)
        self.sleep: dict[str, tuple[time, time]] = {
            "me": scenario.me.sleep,
            "them": scenario.them.sleep,
        }
        # Group-chat friends live where "me" lives and keep ordinary hours.
        for f in scenario.group.friends:
            self.periods[f.key] = scenario.timezones["me"]
            self.sleep[f.key] = (time(0, 0), time(7, 30))
        self._zones: dict[str, ZoneInfo] = {}

    def _zone(self, name: str) -> ZoneInfo:
        if name not in self._zones:
            self._zones[name] = ZoneInfo(name)
        return self._zones[name]

    def tz_at(self, who: str, utc: datetime) -> ZoneInfo:
        current = self.periods[who][0]
        for p in self.periods[who]:
            if p.start_utc <= utc:
                current = p
        return self._zone(current.tz)

    def local(self, who: str, utc: datetime) -> datetime:
        return utc.astimezone(self.tz_at(who, utc))

    def to_utc(self, who: str, local: datetime, after: datetime | None = None) -> datetime:
        """Interpret a naive local datetime in whichever timezone ``who`` was in then.

        Around a timezone switch one wall-clock time can exist twice; the earliest
        reading wins, or the earliest one later than ``after``.
        """
        naive = local.replace(tzinfo=None)
        candidates = []
        for p in self.periods[who]:
            utc = naive.replace(tzinfo=self._zone(p.tz)).astimezone(UTC)
            if self.tz_at(who, utc).key == p.tz and (after is None or utc > after):
                candidates.append(utc)
        if not candidates:  # skipped by a switch; take the nearest sensible instant
            if after is not None:
                return after + timedelta(minutes=1)
            p = self.periods[who][0]
            return naive.replace(tzinfo=self._zone(p.tz)).astimezone(UTC)
        return min(candidates)

    def local_midnight_utc(self, who: str, day: date) -> datetime:
        return self.to_utc(who, datetime.combine(day, time(0, 0)))

    def asleep(self, who: str, utc: datetime) -> bool:
        start, end = self.sleep[who]
        t = self.local(who, utc).time()
        if start <= end:
            return start <= t < end
        return t >= start or t < end

    def next_wake(self, who: str, utc: datetime) -> datetime:
        _, end = self.sleep[who]
        local = self.local(who, utc)
        wake = datetime.combine(local.date(), end)
        if wake <= local.replace(tzinfo=None):
            wake += timedelta(days=1)
        return self.to_utc(who, wake, after=utc)

    def awake_at_or_after(self, who: str, utc: datetime) -> datetime:
        return self.next_wake(who, utc) if self.asleep(who, utc) else utc

    def offset_hours(self, who: str, utc: datetime) -> float:
        off = self.local(who, utc).utcoffset()
        return off.total_seconds() / 3600 if off else 0.0
