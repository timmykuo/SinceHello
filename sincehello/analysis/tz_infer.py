"""Timezone inference from sleep windows (SPEC.md §6.1). Local, deterministic, no LLM.

For each person, over the messages they sent in 1:1 conversations (yours: every 1:1 chat):

1. *Sleep window.* Read every send time on the default timezone's clock (half-hour bins).
   For each day, the 14 days around it give a histogram; its quietest 5 hours (strictly:
   your own night is silent, while the hours the other person sleeps are only quiet)
   are that span's night, and their middle is the night's midpoint.
2. *Baseline.* The most common midpoint is the person's usual night. Each day's shift is
   how far its midpoint moved, in hours ahead (east) of their usual clock, and gives an
   absolute UTC offset (so daylight saving on one side doesn't break a stay in two).
3. *Shift detection.* A run of >= 7 days whose midpoints sit >= 3h off, at a steady
   offset, seeds a candidate. Its start moves to the earliest, and its end to the latest,
   split within 10 days that leaves the fewest sends in the wrong night; the shift is then
   re-read from where the new night is silent. Periods shorter than 7 days or less than
   3h off are dropped, so short trips are ignored.
4. *Home.* Someone else's usual night 5h or more away from yours means they live
   elsewhere: a `home` suggestion covers their whole history at that offset.

When one person is away, the other goes quiet while they sleep too, so a stay can echo on
the other timeline; the weaker of two matching stretches is dropped. Each suggestion has
a UTC offset, a fixed-offset zone (`Etc/GMT-9` is UTC+9) until a place is named, a
confidence, and a travel mention near its start ("landed", "flight", ...) as a hint.
`sync` stores them as `tz_suggestion` rows; `sincehello tz confirm` asks the user.
Results depend only on the store, never on the wall clock.
"""

from __future__ import annotations

import json
import re
import statistics
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from types import EllipsisType
from typing import Any
from zoneinfo import ZoneInfo

from sincehello import config
from sincehello.analysis.tz import TzResolver
from sincehello.store import Store
from sincehello.store.store import ME_PERSON_ID

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

BINS = 48  # half-hour bins
SPAN_DAYS = 14  # rolling span for one sleep-window estimate
MIN_SPAN_MESSAGES = 30  # fewer sends in a span: no estimate
QUIET_BINS = 10  # the night searched for: the quietest 5 hours
NIGHT_BINS = 12  # boundaries test sends against a 6-hour night
MIN_SHIFT_H = 3.0
STABLE_H = 3.0  # estimates this close to a run's offset extend it
MAX_GAP_DAYS = 7  # days of missing or stray estimates a run can bridge
MIN_RUN_ESTIMATES = 7  # a midpoint shifted for >= 7 days seeds a candidate
MIN_DAYS = 7  # shortest suggested period
REFINE_DAYS = 10  # how far a boundary can move from where its run starts or ends
HOME_MIN_H = 5.0  # another person's usual night this far from yours: they live elsewhere
NIGHT_HOURS = (0, 7)  # fallback reference night for "me" with no estimates

TRAVEL = re.compile(
    r"\b(landed|landing|flight|flights|flew|airport|arrived|jet\s?lag\w*|time difference"
    r"|layover|boarding|moved to|moving to|made it to|i'?m home|i'?m back|back in"
    r"|welcome (?:back|home))\b",
    re.IGNORECASE,
)
HINT_DAYS = 3


@dataclass
class Suggestion:
    person_id: int
    kind: str  # "shift" (a stretch away from the usual clock) | "home" (lives elsewhere)
    start_utc: int  # ms
    end_utc: int | None  # ms; None = still there at the end of the history
    utc_offset_hours: float
    iana_tz: str
    confidence: str  # high | medium | low
    score: float
    details: dict[str, Any] = field(default_factory=dict)
    hint_message_id: str | None = None
    hint_quote: str | None = None
    place: str | None = None
    status: str = "pending"
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"] = _iso(self.start_utc)
        d["end"] = _iso(self.end_utc) if self.end_utc is not None else None
        return d

    @classmethod
    def from_row(cls, row: Any) -> Suggestion:
        return cls(
            person_id=row["person_id"],
            kind=row["kind"],
            start_utc=row["start_utc"],
            end_utc=row["end_utc"],
            utc_offset_hours=row["utc_offset_hours"],
            iana_tz=row["iana_tz"],
            confidence=row["confidence"],
            score=row["score"],
            details=json.loads(row["details"] or "{}"),
            hint_message_id=row["hint_message_id"],
            hint_quote=row["hint_quote"],
            place=row["place"],
            status=row["status"],
            id=row["id"],
        )


# --- the per-person timeline ---------------------------------------------------------


@dataclass(frozen=True)
class _Sends:
    """One person's send times on the default clock."""

    ms: list[int]
    day: list[int]  # local date ordinal
    bin: list[int]  # local half-hour bin


def _sends(times: list[int], zone: ZoneInfo) -> _Sends:
    days, bins = [], []
    for t in times:
        local = datetime.fromtimestamp(t / 1000, UTC).astimezone(zone)
        days.append(local.toordinal())
        bins.append(local.hour * 2 + local.minute // 30)
    return _Sends(times, days, bins)


def _quiet_midpoint(hist: list[int]) -> float:
    """Middle (hours) of the night in one span's histogram of send times.

    The night is the quietest QUIET_BINS-wide stretch, strictly: apart from their own
    night, someone far from the person they text also goes quiet while that person
    sleeps, but not silent (a "good morning" waiting for them, a late double text), so
    the quieter of the two is their own. Equally quiet starts form runs; the middle of
    the longest one wins, so a long empty night gives its centre.
    """
    sums = [sum(hist[(b + i) % BINS] for i in range(QUIET_BINS)) for b in range(BINS)]
    best = min(sums)
    tied = [x == best for x in sums]
    if all(tied):
        return 4.0
    # Rotate so no run of tied starts wraps around: begin just after an untied start.
    first = next(b for b in range(BINS) if not tied[b])
    runs: list[tuple[int, int]] = []  # (length, start)
    length = 0
    for k in range(1, BINS + 1):
        b = (first + k) % BINS
        if tied[b]:
            length += 1
        elif length:
            runs.append((length, (b - length) % BINS))
            length = 0
    length, start = max(runs, key=lambda r: (r[0], -r[1]))
    centre_bin = start + (length - 1) / 2 + QUIET_BINS / 2
    return (centre_bin / 2) % 24


def _midpoints(s: _Sends) -> dict[int, float]:
    """Local date ordinal -> quiet-window midpoint (hours) of the 14 days around it."""
    if not s.ms:
        return {}
    per_day: dict[int, list[int]] = {}
    for d, b in zip(s.day, s.bin, strict=True):
        per_day.setdefault(d, [0] * BINS)[b] += 1
    first, last = min(per_day), max(per_day)
    half = SPAN_DAYS // 2
    span = [0] * BINS
    count = 0
    out: dict[int, float] = {}
    # Sliding window over days [d - half, d + half).
    for d in range(first - half, first + half):
        for b, n in enumerate(per_day.get(d, ())):
            span[b] += n
            count += n
    for d in range(first, last + 1):
        if count >= MIN_SPAN_MESSAGES:
            out[d] = _quiet_midpoint(span)
        for b, n in enumerate(per_day.get(d - half, ())):
            span[b] -= n
            count -= n
        for b, n in enumerate(per_day.get(d + half, ())):
            span[b] += n
            count += n
    return out


def _wrap(h: float) -> float:
    """Hours into (-12, 12]."""
    h = h % 24
    return h - 24 if h > 12 else h


def _usual_midpoint(mids: dict[int, float]) -> float | None:
    """The most common midpoint (half-hour resolution, lightly smoothed)."""
    if not mids:
        return None
    counts = Counter(round(m * 2) % BINS for m in mids.values())
    smooth = [2 * counts[b] + counts[(b - 1) % BINS] + counts[(b + 1) % BINS] for b in range(BINS)]
    return smooth.index(max(smooth)) / 2


def _offset_on(zone: ZoneInfo, day: int) -> float:
    noon = datetime.combine(date.fromordinal(day), time(12), tzinfo=zone)
    off = noon.utcoffset()
    return off.total_seconds() / 3600 if off else 0.0


def _normalize_offset(h: float) -> float:
    """A UTC offset into [-12, 14]."""
    while h < -12:
        h += 24
    while h > 14:
        h -= 24
    return h


def fixed_zone(offset_hours: float) -> str:
    """The IANA fixed-offset zone for a whole-hour UTC offset (POSIX sign: Etc/GMT-9 = +9)."""
    n = round(offset_hours)
    return "Etc/UTC" if n == 0 else f"Etc/GMT{-n:+d}"


def _local_ms(zone: ZoneInfo, day: int) -> int:
    return int(datetime.combine(date.fromordinal(day), time(0), tzinfo=zone).timestamp() * 1000)


@dataclass
class _Run:
    days: list[int]
    offsets: list[float]  # absolute UTC offset estimate per day
    shifts: list[float]  # hours ahead of the usual clock per day

    @property
    def offset(self) -> float:
        ref = self.offsets[0]
        return _normalize_offset(ref + statistics.median(_wrap(o - ref) for o in self.offsets))

    @property
    def shift(self) -> float:
        ref = self.shifts[0]
        return _wrap(ref + statistics.median(_wrap(x - ref) for x in self.shifts))


def _runs(mids: dict[int, float], usual: float, zone: ZoneInfo, home_shift: float) -> list[_Run]:
    """Stretches of days whose UTC offset sits >= 3h from the usual one and holds steady.

    Offsets, not clock shifts, so a stay somewhere without daylight saving doesn't break
    in two when the default zone changes its clocks.
    """
    runs: list[_Run] = []
    cur: _Run | None = None
    for d in sorted(mids):
        shift = _wrap(usual - mids[d])  # hours ahead of the usual clock
        offset = _normalize_offset(_offset_on(zone, d) + home_shift + shift)
        fits = abs(shift) >= MIN_SHIFT_H
        if cur is not None and d - cur.days[-1] > MAX_GAP_DAYS + 1:
            runs.append(cur)
            cur = None
        if not fits:
            continue  # a stray estimate; the run ends only after MAX_GAP_DAYS of them
        if cur is not None and abs(_wrap(offset - cur.offset)) > STABLE_H:
            runs.append(cur)
            cur = None
        if cur is None:
            cur = _Run([], [], [])
        cur.days.append(d)
        cur.offsets.append(offset)
        cur.shifts.append(shift)
    if cur is not None:
        runs.append(cur)
    return [r for r in runs if len(r.days) >= MIN_RUN_ESTIMATES]


def _night_bins(mid: float) -> set[int]:
    c = round(mid * 2)
    return {(c + i) % BINS for i in range(-NIGHT_BINS // 2, NIGHT_BINS // 2)}


def _split(
    s: _Sends, lo: int, hi: int, before: set[int], after: set[int], *, latest: bool
) -> int | None:
    """The boundary among sends [lo, hi) that leaves the fewest sends in the wrong night.

    Sends before it are checked against the `before` night bins, the rest against
    `after` (nobody texts while asleep). Equally good splits form a stretch with no
    telling sends; the boundary goes at its earliest point, or its latest with `latest`.
    Callers pick the end that keeps the stretch away short: the usual night is a weak
    witness while away, since the person at home sleeps then too and the traveller goes
    quiet, while the hours that are night away and day at home are busy until the
    traveller leaves and from the moment they're back. None means past the last send.
    """
    wrong_after = [0] * (hi - lo + 1)  # sends at index >= k in the `after` night
    for k in range(hi - 1, lo - 1, -1):
        wrong_after[k - lo] = wrong_after[k - lo + 1] + (s.bin[k] in after)
    costs, wrong_before = [], 0
    for k in range(lo, hi + 1):
        costs.append(wrong_before + wrong_after[k - lo])
        if k < hi:
            wrong_before += s.bin[k] in before
    best = min(costs)
    ties = [lo + i for i, c in enumerate(costs) if c == best]
    return _boundary(s, ties[-1] if latest else ties[0])


def _boundary(s: _Sends, k: int) -> int | None:
    """The instant between sends k-1 and k; None past the last send."""
    if k >= len(s.ms):
        return None
    return s.ms[0] if k == 0 else (s.ms[k - 1] + s.ms[k]) // 2


def _index(s: _Sends, ms: int) -> int:
    return bisect_left(s.ms, ms)


def _confidence(days: float, per_day: float, coverage: float, silence: float) -> tuple[str, float]:
    """high: two weeks or more, enough messages, consistently estimated, a silent new night.

    `coverage` is the share of the period's days whose own estimate agreed; `silence`
    how much quieter the new night's hours got than usual (1 = no sends at all). Shift
    work, exam weeks and sparse data score low on one or the other.
    """
    score = min(1.0, per_day / 5) * coverage * max(0.0, silence)
    if days >= 14 and per_day >= 3 and coverage >= 0.7 and silence >= 0.8:
        level = "high"
    elif per_day < 1 or coverage < 0.5 or silence < 0.5:
        level = "low"
    else:
        level = "medium"
    return level, round(score, 3)


def _refine_shift(s: _Sends, a: int, b: int, usual: float, rough: float) -> float:
    """The shift (hours) whose night is silent over sends [a, b), furthest from home.

    While one person is away, the other's night is quiet too, so when the two nights
    overlap the 14-day midpoints land between them and understate the shift. Sliding
    the night window out from the rough estimate, the silent positions run from the
    overlap to the far edge of the traveller's own night; the far end, less half an
    hour of slack for a night longer than the window, is the shift.
    """
    candidates = [rough + k / 2 for k in range(-6, 7)]
    cost = {c: sum(s.bin[k] in _night_bins(usual - c) for k in range(a, b)) for c in candidates}
    best = min(cost.values())
    quiet = [c for c in candidates if cost[c] <= best]
    far = max(quiet, key=lambda c: abs(_wrap(c)))
    return _wrap(far - 0.5 if _wrap(far) > 0 else far + 0.5)


def _silence(s: _Sends, a: int, b: int, night: set[int]) -> float:
    """1 - (share of sends [a, b) in `night` bins) / (that share in all other sends)."""
    inside = sum(s.bin[k] in night for k in range(a, b)) / max(1, b - a)
    rest = len(s.bin) - (b - a)
    outside = (sum(x in night for x in s.bin) - inside * (b - a)) / max(1, rest)
    return 0.0 if outside <= 0 else max(-1.0, 1 - inside / outside)


def _detect_person(
    s: _Sends, zone: ZoneInfo, person_id: int, reference: float | None
) -> list[Suggestion]:
    mids = _midpoints(s)
    usual = _usual_midpoint(mids)
    if usual is None:
        return []
    out: list[Suggestion] = []

    home_shift = 0.0
    if reference is not None and abs(_wrap(reference - usual)) >= HOME_MIN_H:
        home_shift = float(round(_wrap(reference - usual)))
        offset = Counter(
            _normalize_offset(_offset_on(zone, d) + home_shift) for d in mids
        ).most_common(1)[0][0]
        # A different chronotype can look like a timezone; only a big gap is trusted.
        level = "high" if abs(home_shift) >= 7 and len(mids) >= 28 else "medium"
        score = round(min(1.0, abs(home_shift) / 9) * min(1.0, len(mids) / 60), 3)
        out.append(
            Suggestion(
                person_id,
                "home",
                s.ms[0],
                None,
                offset,
                fixed_zone(offset),
                level,
                score,
                {
                    "usual_night_mid": usual,
                    "your_night_mid": reference,
                    "hours_from_default": home_shift,
                    "messages": len(s.ms),
                },
            )
        )

    runs = _runs(mids, usual, zone, home_shift)
    usual_night = _night_bins(usual)
    prev_end = 0
    for i, run in enumerate(runs):
        shifted_night = _night_bins(usual - run.shift)
        lo = max(_index(s, _local_ms(zone, run.days[0] - REFINE_DAYS)), prev_end)
        hi = max(lo, _index(s, _local_ms(zone, run.days[0] + REFINE_DAYS)))
        start = _split(s, lo, hi, usual_night, shifted_night, latest=False)
        if start is None:
            continue
        lo = max(_index(s, _local_ms(zone, run.days[-1] - REFINE_DAYS)), _index(s, start))
        hi = _index(s, _local_ms(zone, run.days[-1] + 1 + REFINE_DAYS))
        if i + 1 < len(runs):
            hi = min(hi, _index(s, _local_ms(zone, runs[i + 1].days[0] - REFINE_DAYS)))
        end = _split(s, lo, max(lo, hi), shifted_night, usual_night, latest=True)
        last = s.ms[-1] if end is None else end
        days = (last - start) / DAY_MS
        k_start, k_end = _index(s, start), _index(s, last) if end is not None else len(s.ms)
        if days < MIN_DAYS or k_end <= k_start:
            continue
        prev_end = k_end

        n = k_end - k_start
        shift = _refine_shift(s, k_start, k_end, usual, run.shift)
        if abs(shift) < MIN_SHIFT_H:
            continue
        shifted_night = _night_bins(usual - shift)
        silence = _silence(s, k_start, k_end, shifted_night)
        first_day = datetime.fromtimestamp(start / 1000, zone).toordinal()
        last_day = datetime.fromtimestamp(last / 1000, zone).toordinal()
        covered = sum(first_day <= d <= last_day for d in run.days)
        coverage = min(1.0, covered / max(1, last_day - first_day + 1))
        level, score = _confidence(days, n / max(days, 1), coverage, silence)
        offset = float(round(run.offset + _wrap(shift - run.shift)))
        out.append(
            Suggestion(
                person_id,
                "shift",
                start,
                end,
                offset,
                fixed_zone(offset),
                level,
                score,
                {
                    "hours_from_usual": round(shift, 1),
                    "usual_night_mid": usual,
                    "days": round(days, 1),
                    "messages": n,
                    "coverage": round(coverage, 2),
                    "silence": round(silence, 3),
                },
            )
        )
    return _merge_touching(out)


def _merge_touching(found: list[Suggestion]) -> list[Suggestion]:
    """Join consecutive shift periods that touch and sit within STABLE_H of each other.

    One long stay can come out in two pieces when its estimates wander for a week (a
    daylight-saving change on one side only, a quiet stretch). The merged period keeps
    the better-supported piece's offset and day-weighted coverage and silence.
    """
    out: list[Suggestion] = []
    for sg in found:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.kind == sg.kind == "shift"
            and prev.end_utc is not None
            and sg.start_utc - prev.end_utc <= MAX_GAP_DAYS * DAY_MS
            and abs(_wrap(sg.utc_offset_hours - prev.utc_offset_hours)) <= STABLE_H
        ):
            a, b = prev.details, sg.details
            days = a["days"] + b["days"]
            n = a["messages"] + b["messages"]
            coverage = (a["coverage"] * a["days"] + b["coverage"] * b["days"]) / days
            silence = (a["silence"] * a["days"] + b["silence"] * b["days"]) / days
            best = max((prev, sg), key=lambda x: x.details["coverage"] * x.details["days"])
            end = sg.end_utc
            span = ((end if end is not None else sg.start_utc) - prev.start_utc) / DAY_MS
            level, score = _confidence(span, n / max(span, 1), coverage, silence)
            out[-1] = Suggestion(
                prev.person_id,
                "shift",
                prev.start_utc,
                end,
                best.utc_offset_hours,
                best.iana_tz,
                level,
                score,
                {
                    **best.details,
                    "days": round(days, 1),
                    "messages": n,
                    "coverage": round(coverage, 2),
                    "silence": round(silence, 3),
                },
            )
            continue
        out.append(sg)
    return out


# --- store access ---------------------------------------------------------------------

_MY_SENDS = """
    SELECT m.sent_at_utc FROM message m
      JOIN conversation c ON c.id = m.conversation_id AND c.is_group = 0
     WHERE m.is_from_me = 1 AND m.is_reaction = 0
     ORDER BY m.sent_at_utc
"""
_THEIR_SENDS = """
    SELECT m.sent_at_utc FROM message m
      JOIN conversation c ON c.id = m.conversation_id AND c.is_group = 0
      JOIN handle h ON h.id = m.sender_handle_id
     WHERE m.is_from_me = 0 AND m.is_reaction = 0 AND h.person_id = ?
     ORDER BY m.sent_at_utc
"""
_PAIR_MESSAGES = """
    SELECT m.source, m.source_msg_id, m.is_from_me, m.sent_at_utc, m.text
      FROM message m
      LEFT JOIN handle h ON h.id = m.sender_handle_id
     WHERE m.is_reaction = 0 AND m.text IS NOT NULL
       AND m.sent_at_utc >= ? AND m.sent_at_utc < ?
       AND m.conversation_id IN (
           SELECT c.id FROM conversation c
             JOIN conversation_member cm ON cm.conversation_id = c.id
             JOIN handle hm ON hm.id = cm.handle_id
            WHERE c.is_group = 0 AND hm.person_id = ?)
       AND (m.is_from_me = 1 OR h.person_id = ?)
     ORDER BY m.sent_at_utc, m.id
"""


def _times(store: Store, person_id: int) -> list[int]:
    if person_id == ME_PERSON_ID:
        rows = store.conn.execute(_MY_SENDS)
    else:
        rows = store.conn.execute(_THEIR_SENDS, (person_id,))
    return [r[0] for r in rows]


def detect(store: Store, person_id: int, *, default_tz: str | None = None) -> list[Suggestion]:
    """Suggestions for you and one other person, minus what the store already knows.

    Your timeline comes from everything you sent in 1:1 chats; theirs from what they sent.
    A suggestion the user already answered, or that a `user` / `inferred_confirmed`
    period already covers, is left out.
    """
    tz_name = default_tz or config.default_timezone()
    zone = ZoneInfo(tz_name)
    mine = _sends(_times(store, ME_PERSON_ID), zone)
    theirs = _sends(_times(store, person_id), zone)
    my_usual = _usual_midpoint(_midpoints(mine))
    found = _detect_person(mine, zone, ME_PERSON_ID, None)
    reference = my_usual if my_usual is not None else sum(NIGHT_HOURS) / 2
    found += _detect_person(theirs, zone, person_id, reference)
    found = _drop_echoes(found)

    known = TzResolver(store, tz_name, use_inferred=False)
    out = []
    for sg in found:
        if _already_known(known, sg) or _answered(store, sg):
            continue
        sg.hint_message_id, sg.hint_quote = _hint(store, person_id, sg)
        out.append(sg)
    return out


def _drop_echoes(found: list[Suggestion]) -> list[Suggestion]:
    """Drop one person's shift that only mirrors the other's.

    When one of you is far away, the other also goes quiet while they sleep, so a
    stretch can show up on both timelines at the same offset. The better-supported one
    stays; both stay only if both are well supported (a trip together).
    """

    def support(sg: Suggestion) -> float:
        return sg.details.get("coverage", 0) * max(0.0, sg.details.get("silence", 0))

    out = []
    for sg in found:
        if sg.kind == "shift":
            rivals = [
                o
                for o in found
                if o.kind == "shift"
                and o.person_id != sg.person_id
                and _shared(sg, o) > 0
                and abs(_wrap(o.utc_offset_hours - sg.utc_offset_hours)) <= STABLE_H
            ]
            if any(
                support(o) > support(sg) and not (support(sg) >= 0.6 and support(o) >= 0.6)
                for o in rivals
            ):
                continue
        out.append(sg)
    return out


def _shared(a: Suggestion, b: Suggestion) -> int:
    far = 1 << 62
    a_end = a.end_utc if a.end_utc is not None else far
    b_end = b.end_utc if b.end_utc is not None else far
    return min(a_end, b_end) - max(a.start_utc, b.start_utc)


def _already_known(known: TzResolver, sg: Suggestion) -> bool:
    """A user or confirmed period already gives this offset for most of the stretch."""
    end = sg.end_utc if sg.end_utc is not None else sg.start_utc + 30 * DAY_MS
    probes = [sg.start_utc + (end - sg.start_utc) * k // 10 for k in range(1, 10)]
    hits = 0
    for t in probes:
        if known.source(sg.person_id, t) == "default":
            continue
        off = known.local(sg.person_id, t).utcoffset()
        hours = off.total_seconds() / 3600 if off else 0.0
        hits += abs(_wrap(hours - sg.utc_offset_hours)) <= 1
    return hits >= len(probes) / 2


def _overlaps(a: Suggestion, b: Suggestion) -> bool:
    """The same suggestion again: same kind, over half the shorter one shared, offset within 1h."""
    far = 1 << 62
    shorter = min((x.end_utc if x.end_utc is not None else far) - x.start_utc for x in (a, b))
    shared = _shared(a, b)
    return (
        a.kind == b.kind
        and shared > 0
        and shared >= shorter / 2
        and abs(_wrap(a.utc_offset_hours - b.utc_offset_hours)) <= 1
    )


def _answered(store: Store, sg: Suggestion) -> bool:
    return any(
        _overlaps(sg, Suggestion.from_row(r))
        for r in store.tz_suggestions(sg.person_id)
        if r["status"] != "pending"
    )


def _hint(store: Store, person_id: int, sg: Suggestion) -> tuple[str | None, str | None]:
    """A travel mention near the start (else the end), the traveller's own first."""
    for boundary in (sg.start_utc, sg.end_utc):
        if boundary is None:
            continue
        rows = store.conn.execute(
            _PAIR_MESSAGES,
            (boundary - HINT_DAYS * DAY_MS, boundary + HINT_DAYS * DAY_MS, person_id, person_id),
        ).fetchall()
        hits = [r for r in rows if TRAVEL.search(r["text"])]
        if not hits:
            continue
        own = 1 if sg.person_id == ME_PERSON_ID else 0
        best = min(hits, key=lambda r: (r["is_from_me"] != own, abs(r["sent_at_utc"] - boundary)))
        return f"{best['source']}:{best['source_msg_id']}", best["text"][:200]
    return None, None


def sync(store: Store, person_id: int, *, default_tz: str | None = None) -> list[Suggestion]:
    """Detect, then make the pending suggestions in the store match. Idempotent.

    A pending suggestion that matches a new one is updated in place (keeping any place
    already named for it); pending ones nothing matches any more are removed. Confirmed
    and ignored ones are never touched. Returns the pending suggestions, oldest first.
    """
    found = detect(store, person_id, default_tz=default_tz)
    for pid in (ME_PERSON_ID, person_id):
        pending = [Suggestion.from_row(r) for r in store.tz_suggestions(pid, status="pending")]
        matched: set[int] = set()
        for sg in (f for f in found if f.person_id == pid):
            old = next((p for p in pending if p.id not in matched and _overlaps(sg, p)), None)
            fields = {
                "kind": sg.kind,
                "start_utc": sg.start_utc,
                "end_utc": sg.end_utc,
                "utc_offset_hours": sg.utc_offset_hours,
                "confidence": sg.confidence,
                "score": sg.score,
                "hint_message_id": sg.hint_message_id,
                "hint_quote": sg.hint_quote,
                "details": json.dumps(sg.details, sort_keys=True),
            }
            if old is None:
                store.add_tz_suggestion(pid, iana_tz=sg.iana_tz, **fields)
            else:
                matched.add(old.id)
                if old.place is None:  # a named zone outlives a re-detection
                    fields["iana_tz"] = sg.iana_tz
                store.update_tz_suggestion(old.id, **fields)
        for p in pending:
            if p.id not in matched:
                store.delete_tz_suggestion(p.id)
    return pending_suggestions(store, person_id)


def pending_suggestions(store: Store, person_id: int) -> list[Suggestion]:
    rows = [
        *store.tz_suggestions(ME_PERSON_ID, "pending"),
        *store.tz_suggestions(person_id, "pending"),
    ]
    return sorted((Suggestion.from_row(r) for r in rows), key=lambda s: (s.start_utc, s.person_id))


def accept(
    store: Store,
    sg: Suggestion,
    *,
    iana_tz: str | None = None,
    start_utc: int | None = None,
    end_utc: int | None | EllipsisType = ...,
) -> int:
    """Write a suggestion to `tz_period` and mark it confirmed. Returns the period id.

    As suggested, the period's source is `inferred_confirmed`; with any edit, `user`.
    `end_utc=None` makes it open-ended; leaving it out keeps the suggested end.
    """
    edited = iana_tz is not None or start_utc is not None or end_utc is not ...
    tz = iana_tz or sg.iana_tz
    ZoneInfo(tz)  # fail early on a bad name
    start = sg.start_utc if start_utc is None else start_utc
    end = sg.end_utc if end_utc is ... else end_utc
    if end is not None and end <= start:
        raise ValueError("the period must end after it starts")
    pid = store.add_tz_period(
        sg.person_id,
        datetime.fromtimestamp(start / 1000, UTC),
        datetime.fromtimestamp(end / 1000, UTC) if end is not None else None,
        tz,
        source="user" if edited else "inferred_confirmed",
    )
    if sg.id is not None:
        store.update_tz_suggestion(sg.id, status="confirmed", iana_tz=tz)
    return pid


def ignore(store: Store, sg: Suggestion) -> None:
    if sg.id is not None:
        store.update_tz_suggestion(sg.id, status="ignored")


def offset_label(hours: float) -> str:
    sign = "+" if hours >= 0 else "-"
    h = abs(hours)
    return f"UTC{sign}{int(h):02d}:{round((h % 1) * 60):02d}"


def zone_matches(iana_tz: str, offset_hours: float, at_ms: int, tolerance: float = 1.0) -> bool:
    """Whether a zone's UTC offset at `at_ms` is within `tolerance` hours of an offset."""
    off = datetime.fromtimestamp(at_ms / 1000, UTC).astimezone(ZoneInfo(iana_tz)).utcoffset()
    hours = off.total_seconds() / 3600 if off else 0.0
    return abs(_wrap(hours - offset_hours)) <= tolerance


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def midpoint_ms(sg: Suggestion, last_ms: int | None = None) -> int:
    end = sg.end_utc if sg.end_utc is not None else (last_ms or sg.start_utc + 30 * DAY_MS)
    return (sg.start_utc + end) // 2
