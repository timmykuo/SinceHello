"""Deterministic local stats engine (SPEC.md §7.1). No LLM involved.

Every stat covers 1:1 conversations with one person, reactions excluded, and is keyed by
sender ("me" / "them"). Time-of-day and calendar-day stats use the sender's local time
(SPEC.md §6.1). Each result carries a stable query_id derived from the stat name, person,
and fully resolved parameters, so a verifier can recompute it (SPEC.md §7.6).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from sincehello import config
from sincehello.analysis import text as txt
from sincehello.analysis.dataset import ME, SENDERS, THEM, Dataset
from sincehello.store import Store
from sincehello.store.store import ME_PERSON_ID

# Bump when a stat's definition or parameter names change, so old query ids no longer match.
# 2: the top-N parameter is `top_n` everywhere (was `k` in the language stats).
# 3: cross_timezone adds `zones`, and per window `sources`, `inferred`, `reunion_message_id`.
STATS_VERSION = 3

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# --- results and registry ----------------------------------------------------


@dataclass(frozen=True)
class StatResult:
    query_id: str
    stat: str
    person_id: int
    params: dict[str, Any]
    value: dict[str, Any]
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "stat": self.stat,
            "person_id": self.person_id,
            "params": self.params,
            "value": self.value,
            "flags": list(self.flags),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(frozen=True)
class StatSpec:
    name: str
    func: Callable[..., dict[str, Any] | tuple[dict[str, Any], list[str]]]
    defaults: dict[str, Any]
    local_time: bool  # depends on timezones, so takes a default_tz parameter
    summary: str = field(default="")


STATS: dict[str, StatSpec] = {}


def stat(name: str, *, local_time: bool = False):
    """Register a stat. Its keyword-only arguments, with defaults, are its parameters."""

    def register(func):
        params = inspect.signature(func).parameters.values()
        defaults = {p.name: p.default for p in params if p.kind is p.KEYWORD_ONLY}
        summary = (func.__doc__ or "").strip().splitlines()[0]
        STATS[name] = StatSpec(name, func, defaults, local_time, summary)
        return func

    return register


def query_id(stat_name: str, person_id: int, params: dict[str, Any]) -> str:
    key = {"v": STATS_VERSION, "stat": stat_name, "person_id": person_id, "params": params}
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return f"q_{stat_name}_{digest[:10]}"


def resolve_params(stat_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill in defaults so the params alone determine the result."""
    try:
        spec = STATS[stat_name]
    except KeyError:
        raise ValueError(f"unknown stat {stat_name!r} (known: {', '.join(STATS)})") from None
    allowed: dict[str, Any] = {**spec.defaults, "start_utc": None, "end_utc": None}
    if spec.local_time:
        allowed["default_tz"] = None
    params = params or {}
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        takes = f"it takes: {', '.join(spec.defaults)}" if spec.defaults else "it takes none"
        raise ValueError(f"{stat_name} does not take {', '.join(unknown)}; {takes}")
    resolved = {**allowed, **params}
    if spec.local_time:
        resolved["default_tz"] = resolved["default_tz"] or config.default_timezone()
        ZoneInfo(resolved["default_tz"])  # fail early on a bad name
    return dict(sorted(resolved.items()))


class _Datasets:
    """Load each (window, timezone) once when computing several stats."""

    def __init__(self, store: Store, person_id: int) -> None:
        self.store, self.person_id = store, person_id
        self._cache: dict[tuple[int | None, int | None, str], Dataset] = {}

    def get(self, start: int | None, end: int | None, tz: str | None) -> Dataset:
        if tz is None:  # timezone doesn't matter; reuse any load of this window
            for (s, e, _), ds in self._cache.items():
                if (s, e) == (start, end):
                    return ds
            tz = "UTC"
        key = (start, end, tz)
        if key not in self._cache:
            self._cache[key] = Dataset(self.store, self.person_id, tz, start, end)
        return self._cache[key]


def _run(datasets: _Datasets, stat_name: str, params: dict[str, Any] | None) -> StatResult:
    resolved = resolve_params(stat_name, params)
    spec = STATS[stat_name]
    ds = datasets.get(resolved["start_utc"], resolved["end_utc"], resolved.get("default_tz"))
    own = {k: resolved[k] for k in spec.defaults}
    out = spec.func(ds, **own)
    value, flags = out if isinstance(out, tuple) else (out, [])
    if not ds.messages:
        flags = ["no_messages", *flags]
    return StatResult(
        query_id(stat_name, ds.person_id, resolved),
        stat_name,
        ds.person_id,
        resolved,
        value,
        tuple(flags),
    )


def compute(
    store: Store, person_id: int, stat_name: str, params: dict[str, Any] | None = None
) -> StatResult:
    return _run(_Datasets(store, person_id), stat_name, params)


def compute_all(
    store: Store,
    person_id: int,
    names: Iterable[str] | None = None,
    **shared: Any,
) -> dict[str, StatResult]:
    """Compute several stats. Each shared param goes to every stat that takes it."""
    datasets = _Datasets(store, person_id)
    results = {}
    for name in names or STATS:
        resolve_params(name)  # validates the name
        spec = STATS[name]
        accepted = set(spec.defaults) | {"start_utc", "end_utc"}
        if spec.local_time:
            accepted.add("default_tz")
        results[name] = _run(datasets, name, {k: v for k, v in shared.items() if k in accepted})
    return results


def recompute(store: Store, result: StatResult | dict[str, Any]) -> StatResult:
    """Re-run a stat from its recorded stat name, person, and params (SPEC.md §7.6)."""
    d = result.to_dict() if isinstance(result, StatResult) else result
    fresh = compute(store, d["person_id"], d["stat"], d["params"])
    if fresh.query_id != d["query_id"]:
        raise ValueError(f"query id mismatch: {d['query_id']} vs {fresh.query_id}")
    return fresh


# --- helpers -----------------------------------------------------------------


def _r(x: float | None, digits: int = 4) -> float | None:
    return None if x is None else round(x, digits)


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _senders(counts: Counter) -> dict[str, int]:
    return {s: counts[s] for s in SENDERS}


def _shares(counts: Counter) -> dict[str, float | None]:
    total = sum(counts[s] for s in SENDERS)
    return {s: _r(counts[s] / total) if total else None for s in SENDERS}


def _p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _in_hours(hour: int, start: int, end: int) -> bool:
    """start <= hour < end, wrapping past midnight when start > end."""
    return start <= hour < end if start <= end else hour >= start or hour < end


def _top(counts: Counter, k: int, min_count: int = 1) -> list[list[Any]]:
    ranked = sorted(((t, c) for t, c in counts.items() if c >= min_count), key=_rank)
    return [[t, c] for t, c in ranked[:k]]


def _rank(item: tuple[Any, int]) -> tuple[int, Any]:
    return (-item[1], item[0])


def _per_sender_counts(
    ds: Dataset, extract: Callable[[list[list[str]]], Iterable[str]]
) -> dict[str, Counter]:
    """Count extract(segments) per sender."""
    counts = {s: Counter() for s in SENDERS}
    for m, segs in zip(ds.messages, ds.segments, strict=True):
        counts[m.sender].update(extract(segs))
    return counts


def _top_by_sender(counts: dict[str, Counter], k: int, min_count: int = 1) -> dict[str, Any]:
    both = counts[ME] + counts[THEM]
    return {
        **{s: _top(counts[s], k, min_count) for s in SENDERS},
        "both": _top(both, k, min_count),
    }


def _calendar_diff(start: date, end: date) -> dict[str, int]:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    y, m = divmod(start.month - 1 + months, 12)
    anchor_year, anchor_month = start.year + y, m + 1
    # Clamp the anchor day for short months (e.g. Jan 31 + 1 month).
    next_month = date(anchor_year + anchor_month // 12, anchor_month % 12 + 1, 1)
    anchor = date(anchor_year, anchor_month, min(start.day, (next_month - timedelta(1)).day))
    return {"years": months // 12, "months": months % 12, "days": (end - anchor).days}


# --- volume and rhythm -------------------------------------------------------


@stat("totals", local_time=True)
def totals(ds: Dataset) -> dict[str, Any]:
    """Message totals per sender, per year, and per month (sender's local time)."""
    by_sender = Counter(m.sender for m in ds.messages)
    by_year: dict[str, Counter] = defaultdict(Counter)
    by_month: dict[str, Counter] = defaultdict(Counter)
    for m, local in zip(ds.messages, ds.local, strict=True):
        by_year[f"{local:%Y}"][m.sender] += 1
        by_month[f"{local:%Y-%m}"][m.sender] += 1
    return {
        "total": len(ds.messages),
        "by_sender": _senders(by_sender),
        "share": _shares(by_sender),
        "by_year": {k: _senders(v) for k, v in sorted(by_year.items())},
        "by_month": {k: _senders(v) for k, v in sorted(by_month.items())},
    }


@stat("heatmap", local_time=True)
def heatmap(ds: Dataset) -> dict[str, Any]:
    """Weekday x hour message counts per sender, in the sender's local time."""
    grid = {s: [[0] * 24 for _ in WEEKDAYS] for s in SENDERS}
    for m, local in zip(ds.messages, ds.local, strict=True):
        grid[m.sender][local.weekday()][local.hour] += 1
    grid["total"] = [
        [grid[ME][d][h] + grid[THEM][d][h] for h in range(24)] for d in range(len(WEEKDAYS))
    ]
    peak: dict[str, Any] = {}
    for who, cells in grid.items():
        best = max(
            ((cells[d][h], -d, -h) for d in range(len(WEEKDAYS)) for h in range(24)),
        )
        count, d, h = best[0], -best[1], -best[2]
        peak[who] = {"weekday": WEEKDAYS[d], "hour": h, "count": count} if count else None
    return {"weekdays": list(WEEKDAYS), "grid": grid, "peak": peak}


@stat("first_message", local_time=True)
def first_message(ds: Dataset, *, as_of_utc: int | None = None) -> dict[str, Any]:
    """First message ever, who sent it, and the "since hello" duration.

    The duration runs to as_of_utc (ms), or to the last message when it is None, so the
    result never depends on the wall clock.
    """
    if not ds.messages:
        return {"message_id": None, "sender": None, "since_hello_days": None}
    first, first_local = ds.messages[0], ds.local[0]
    end_ms = as_of_utc if as_of_utc is not None else ds.messages[-1].sent_at_utc
    end_local = datetime.fromtimestamp(end_ms / 1000, UTC).astimezone(first_local.tzinfo)
    return {
        "message_id": first.message_id,
        "sender": first.sender,
        "sent_at_utc": _iso(first.sent_at_utc),
        "sent_at_local": first_local.isoformat(timespec="seconds"),
        "local_tz": str(first_local.tzinfo),
        "as_of_utc": _iso(end_ms),
        "since_hello_days": (end_ms - first.sent_at_utc) // DAY_MS,
        "since_hello": _calendar_diff(first_local.date(), end_local.date()),
    }


@stat("busiest_day", local_time=True)
def busiest_day(ds: Dataset, *, top_n: int = 5) -> dict[str, Any]:
    """The day with the most messages. Each message counts on its sender's local date."""
    days: dict[str, Counter] = defaultdict(Counter)
    for m, local in zip(ds.messages, ds.local, strict=True):
        days[local.date().isoformat()][m.sender] += 1
    ranked = sorted(days.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    if not ranked:
        return {"date": None, "count": 0, "by_sender": _senders(Counter()), "top": []}
    day, counts = ranked[0]
    return {
        "date": day,
        "count": sum(counts.values()),
        "by_sender": _senders(counts),
        "top": [{"date": d, "count": sum(c.values())} for d, c in ranked[:top_n]],
    }


@stat("longest_silence")
def longest_silence(ds: Dataset) -> dict[str, Any]:
    """The longest gap between two consecutive messages, and who broke it."""
    best = None
    for prev, cur in pairwise(ds.messages):
        gap = cur.sent_at_utc - prev.sent_at_utc
        if best is None or gap > best[0]:
            best = (gap, prev, cur)
    if best is None:
        return {"hours": None, "days": None}
    gap, prev, cur = best
    return {
        "hours": _r(gap / HOUR_MS),
        "days": _r(gap / DAY_MS),
        "start_utc": _iso(prev.sent_at_utc),
        "end_utc": _iso(cur.sent_at_utc),
        "last_message_id": prev.message_id,
        "next_message_id": cur.message_id,
        "broken_by": cur.sender,
    }


@stat("longest_streak", local_time=True)
def longest_streak(ds: Dataset) -> dict[str, Any]:
    """The longest run of consecutive days with at least one message (sender-local dates)."""
    days = sorted({local.date() for local in ds.local})
    if not days:
        return {"days": 0, "start_date": None, "end_date": None, "active_days": 0}
    best = (1, days[0], days[0])
    run_start = days[0]
    for prev, cur in pairwise(days):
        if cur - prev != timedelta(days=1):
            run_start = cur
        length = (cur - run_start).days + 1
        if length > best[0]:
            best = (length, run_start, cur)
    return {
        "days": best[0],
        "start_date": best[1].isoformat(),
        "end_date": best[2].isoformat(),
        "active_days": len(days),
    }


@stat("who_texts_first")
def who_texts_first(ds: Dataset, *, gap_hours: float = 6.0) -> dict[str, Any]:
    """Who starts conversations: the first message, and any after a gap of >= gap_hours."""
    starts: Counter = Counter()
    prev = None
    for m in ds.messages:
        if prev is None or m.sent_at_utc - prev.sent_at_utc >= gap_hours * HOUR_MS:
            starts[m.sender] += 1
        prev = m
    return {"starts": sum(starts.values()), "by_sender": _senders(starts), "share": _shares(starts)}


@stat("reply_times")
def reply_times(ds: Dataset, *, max_reply_hours: float = 6.0) -> dict[str, Any]:
    """Reply-time distribution per sender (median, nearest-rank p90), in seconds.

    A reply is a message whose previous message came from the other person less than
    max_reply_hours earlier; longer gaps count as new conversations instead.
    """
    gaps: dict[str, list[float]] = {s: [] for s in SENDERS}
    for prev, cur in pairwise(ds.messages):
        gap = cur.sent_at_utc - prev.sent_at_utc
        if cur.sender != prev.sender and gap < max_reply_hours * HOUR_MS:
            gaps[cur.sender].append(gap / 1000)
    return {
        s: {
            "replies": len(g),
            "median_seconds": _r(statistics.median(g)) if g else None,
            "p90_seconds": _r(_p90(g)) if g else None,
        }
        for s, g in gaps.items()
    }


@stat("late_night", local_time=True)
def late_night(ds: Dataset, *, start_hour: int = 0, end_hour: int = 4) -> dict[str, Any]:
    """Share of messages sent between start_hour and end_hour, sender's local time."""
    late: Counter = Counter()
    sent = Counter(m.sender for m in ds.messages)
    for m, local in zip(ds.messages, ds.local, strict=True):
        if _in_hours(local.hour, start_hour, end_hour):
            late[m.sender] += 1

    def block(n: int, total: int) -> dict[str, Any]:
        return {"late": n, "total": total, "share": _r(n / total) if total else None}

    return {
        **{s: block(late[s], sent[s]) for s in SENDERS},
        "overall": block(sum(late.values()), len(ds.messages)),
    }


@stat("message_length")
def message_length(ds: Dataset) -> dict[str, Any]:
    """Message length per sender, over messages with text."""
    out: dict[str, Any] = {}
    for s in SENDERS:
        msgs = [m for m in ds.messages if m.sender == s and m.text.strip()]
        if not msgs:
            out[s] = {"messages": 0, "mean_chars": None, "median_chars": None, "mean_words": None}
            continue
        chars = [len(m.text) for m in msgs]
        longest = max(msgs, key=lambda m: len(m.text))
        out[s] = {
            "messages": len(msgs),
            "mean_chars": _r(statistics.fmean(chars)),
            "median_chars": _r(statistics.median(chars)),
            "mean_words": _r(statistics.fmean(len(m.text.split()) for m in msgs)),
            "longest": {"chars": len(longest.text), "message_id": longest.message_id},
        }
    return out


@stat("double_texting")
def double_texting(ds: Dataset, *, min_gap_minutes: float = 20.0) -> dict[str, Any]:
    """Double-texter score: follow-ups sent >= min_gap_minutes after your own unanswered message.

    double_text_rate = double texts / turns, where a turn is a run of one sender's messages.
    """
    sent: Counter = Counter()
    turns: Counter = Counter()
    doubles: Counter = Counter()
    prev = None
    for m in ds.messages:
        sent[m.sender] += 1
        if prev is None or prev.sender != m.sender:
            turns[m.sender] += 1
        elif m.sent_at_utc - prev.sent_at_utc >= min_gap_minutes * 60_000:
            doubles[m.sender] += 1
        prev = m
    rate = {s: doubles[s] / turns[s] if turns[s] else None for s in SENDERS}
    out: dict[str, Any] = {
        s: {
            "messages": sent[s],
            "turns": turns[s],
            "messages_per_turn": _r(sent[s] / turns[s]) if turns[s] else None,
            "double_texts": doubles[s],
            "double_text_rate": _r(rate[s]),
        }
        for s in SENDERS
    }
    me_rate, them_rate = rate[ME] or 0.0, rate[THEM] or 0.0
    out["double_texter"] = None if me_rate == them_rate else (ME if me_rate > them_rate else THEM)
    return out


# --- language ----------------------------------------------------------------


@stat("top_words")
def top_words(ds: Dataset, *, top_n: int = 20) -> dict[str, Any]:
    """Most-used words per sender, stopwords removed (English)."""
    return _top_by_sender(_per_sender_counts(ds, txt.words), top_n)


@stat("top_ngrams")
def top_ngrams(ds: Dataset, *, top_n: int = 10, min_count: int = 2) -> dict[str, Any]:
    """Most-used bigrams and trigrams per sender, skipping ones made only of stopwords."""
    out = {}
    for n, label in ((2, "bigrams"), (3, "trigrams")):
        counts = _per_sender_counts(ds, lambda t, n=n: (" ".join(g) for g in txt.ngrams(t, n)))
        out[label] = _top_by_sender(counts, top_n, min_count)
    return out


@stat("top_emoji")
def top_emoji(ds: Dataset, *, top_n: int = 10) -> dict[str, Any]:
    """Most-used emoji per sender."""
    counts = {s: Counter() for s in SENDERS}
    for m in ds.messages:
        counts[m.sender].update(txt.emoji(m.text))
    return _top_by_sender(counts, top_n)


def _phrase_grams(segments: list[list[str]], max_n: int) -> Iterable[tuple[str, ...]]:
    """Candidate phrases: 1..max_n-grams of words that neither start nor end with a stopword."""
    stop = txt.stopwords()
    for seg in segments:
        for n in range(1, max_n + 1):
            for i in range(len(seg) - n + 1):
                gram = tuple(seg[i : i + n])
                if gram[0] in stop or gram[-1] in stop or not all(map(txt.is_word, gram)):
                    continue
                yield gram


def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    n = len(shorter)
    return any(longer[i : i + n] == shorter for i in range(len(longer) - n + 1))


@stat("distinctive_phrases")
def distinctive_phrases(
    ds: Dataset,
    *,
    top_n: int = 20,
    max_n: int = 3,
    min_count: int = 3,
    min_ratio: float = 5.0,
    min_baseline_tokens: int = 500,
) -> tuple[dict[str, Any], list[str]]:
    """Phrases frequent here but rare in a baseline: prime inside-joke candidates.

    The baseline is my 1:1 conversations with everyone else. With fewer than
    min_baseline_tokens there, it falls back to the bundled common-English word list:
    a phrase then qualifies by having a word outside that list, ranked by count.
    """
    counts: Counter = Counter()
    by_sender: dict[str, Counter] = {s: Counter() for s in SENDERS}
    n_tokens = 0
    for m, segs in zip(ds.messages, ds.segments, strict=True):
        n_tokens += sum(map(len, segs))
        grams = list(_phrase_grams(segs, max_n))
        counts.update(grams)
        by_sender[m.sender].update(grams)

    base: Counter = Counter()
    base_tokens = 0
    for t in ds.baseline_texts():
        segs = txt.segments(t)
        base_tokens += sum(map(len, segs))
        base.update(_phrase_grams(segs, max_n))

    flags: list[str] = []
    ratios: dict[tuple[str, ...], float | None] = {}
    if base_tokens >= min_baseline_tokens:
        mode = "other_conversations"
        for g, c in counts.items():
            ratio = (c / n_tokens) / ((base[g] + 1) / (base_tokens + 1))
            if c >= min_count and ratio >= min_ratio:
                ratios[g] = ratio
        ranked = sorted(ratios, key=lambda g: (-ratios[g], -counts[g], g))
    else:
        mode = "general_english"
        flags.append("baseline_general_english")
        common = txt.common_english()
        for g, c in counts.items():
            if c >= min_count and any(t not in common for t in g):
                ratios[g] = None
        ranked = sorted(ratios, key=lambda g: (-counts[g], -len(g), g))

    # Drop a phrase when a longer kept phrase contains it and explains most of its uses
    # ("pickle" inside "pickle rick").
    kept: list[tuple[str, ...]] = []
    for g in sorted(ranked, key=len, reverse=True):
        if not any(
            len(o) > len(g) and _contains(o, g) and counts[o] >= 0.8 * counts[g] for o in kept
        ):
            kept.append(g)
    keep = set(kept)
    phrases = [
        {
            "phrase": " ".join(g),
            "n": len(g),
            "count": counts[g],
            "by_sender": _senders(Counter({s: by_sender[s][g] for s in SENDERS})),
            "baseline_count": base[g] if mode == "other_conversations" else None,
            "ratio": _r(ratios[g], 2) if ratios[g] is not None else None,
        }
        for g in ranked
        if g in keep
    ][:top_n]
    if not phrases:
        flags.append("none_found")
    return {"baseline": mode, "baseline_tokens": base_tokens, "phrases": phrases}, flags


# --- cross-timezone (SPEC.md §6.1) -------------------------------------------


def _hour_ranges(hours: list[int]) -> list[tuple[int, int]]:
    """Group UTC hours into circular contiguous [start, end) ranges."""
    hs = set(hours)
    if not hs:
        return []
    if len(hs) == 24:
        return [(0, 24)]
    starts = [h for h in sorted(hs) if (h - 1) % 24 not in hs]
    ranges = []
    for s in starts:
        n = 0
        while (s + n) % 24 in hs:
            n += 1
        ranges.append((s, s + n))
    return ranges


def _fmt_range(start_local: datetime, hours: int) -> str:
    end = start_local + timedelta(hours=hours)
    return f"{start_local:%H:00}-{end:%H:00}"


@stat("cross_timezone", local_time=True)
def cross_timezone(
    ds: Dataset,
    *,
    min_diff_hours: float = 3.0,
    min_days: int = 28,
    night_start: int = 23,
    night_end: int = 7,
    awake_start: int = 8,
    awake_end: int = 23,
) -> tuple[dict[str, Any], list[str]]:
    """Stretches where your timezones differ by >= min_diff_hours for >= min_days.

    For each: the waking-hours overlap, messages sent during the recipient's night, how
    long those night messages waited for a reply, and the first message after it ends
    (the reunion). `sources` says where each zone came from; `inferred` marks a window
    that rests on a timezone the user hasn't confirmed (SPEC.md §6.1). `zones` counts
    each sender's messages by the zone they were sent from.
    """
    tz = ds.tz
    zones: dict[str, Counter] = {s: Counter() for s in SENDERS}
    for m, local in zip(ds.messages, ds.local, strict=True):
        zones[m.sender][str(local.tzinfo)] += 1
    by_zone = {s: [{"tz": z, "messages": n} for z, n in c.most_common()] for s, c in zones.items()}
    if not ds.messages or not (tz.has_periods(ME_PERSON_ID) or tz.has_periods(ds.person_id)):
        return {"windows": [], "zones": by_zone}, ["no_cross_timezone_periods"]

    def offset_hours(person: int, ms: int) -> float:
        local = tz.local(person, ms)
        return local.utcoffset().total_seconds() / 3600

    first_day = datetime.fromtimestamp(ds.messages[0].sent_at_utc / 1000, UTC).date()
    last_day = datetime.fromtimestamp(ds.messages[-1].sent_at_utc / 1000, UTC).date()
    runs: list[list[tuple[date, float]]] = []
    current: list[tuple[date, float]] = []
    day = first_day
    while day <= last_day:
        noon = int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)
        diff = offset_hours(ds.person_id, noon) - offset_hours(ME_PERSON_ID, noon)
        if abs(diff) >= min_diff_hours:
            current.append((day, diff))
        elif current:
            runs.append(current)
            current = []
        day += timedelta(days=1)
    if current:
        runs.append(current)

    windows = []
    for run in (r for r in runs if len(r) >= min_days):
        start_day, end_day = run[0][0], run[-1][0]
        start_ms = int(datetime(*start_day.timetuple()[:3], tzinfo=UTC).timestamp() * 1000)
        end_ms = int(datetime(*end_day.timetuple()[:3], tzinfo=UTC).timestamp() * 1000) + DAY_MS
        msgs = [m for m in ds.messages if start_ms <= m.sent_at_utc < end_ms]
        mid_day = run[len(run) // 2][0]
        mid = int(datetime(*mid_day.timetuple()[:3], tzinfo=UTC).timestamp() * 1000)

        both_awake = [
            h
            for h in range(24)
            if all(
                _in_hours(tz.local(p, mid + h * HOUR_MS).hour, awake_start, awake_end)
                for p in (ME_PERSON_ID, ds.person_id)
            )
        ]
        overlap = [
            {
                ME: _fmt_range(tz.local(ME_PERSON_ID, mid + s * HOUR_MS), e - s),
                THEM: _fmt_range(tz.local(ds.person_id, mid + s * HOUR_MS), e - s),
                "hours": e - s,
            }
            for s, e in _hour_ranges(both_awake)
        ]

        night: Counter = Counter()
        waits: dict[str, list[float]] = {s: [] for s in SENDERS}
        for i, m in enumerate(msgs):
            recipient = ds.person_id if m.sender == ME else ME_PERSON_ID
            if not _in_hours(tz.local(recipient, m.sent_at_utc).hour, night_start, night_end):
                continue
            night[m.sender] += 1
            # Last message of the sender's turn, sent during the recipient's night: how
            # long until the recipient answered?
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            if nxt is not None and nxt.sender != m.sender:
                waits[m.sender].append((nxt.sent_at_utc - m.sent_at_utc) / HOUR_MS)

        sources = {ME: tz.source(ME_PERSON_ID, mid), THEM: tz.source(ds.person_id, mid)}
        after = next((m for m in ds.messages if m.sent_at_utc >= end_ms), None)
        windows.append(
            {
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
                "days": len(run),
                "me_tz": str(tz.zone(ME_PERSON_ID, mid)),
                "them_tz": str(tz.zone(ds.person_id, mid)),
                "sources": sources,
                "inferred": "inferred" in sources.values(),
                "reunion_message_id": after.message_id if after else None,
                "them_ahead_hours": _r(statistics.median(d for _, d in run), 2),
                "messages": len(msgs),
                "overlap": overlap,
                "sent_during_recipient_night": _senders(night),
                "night_reply_wait_hours_median": {
                    s: _r(statistics.median(w), 2) if w else None for s, w in waits.items()
                },
            }
        )
    flags = [] if windows else ["no_cross_timezone_periods"]
    if any(w["inferred"] for w in windows):
        flags.append("inferred_timezones")
    return {"windows": windows, "zones": by_zone}, flags
