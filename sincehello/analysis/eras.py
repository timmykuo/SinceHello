"""Era detection (SPEC.md §7.2): split a relationship into chapters, locally, no LLM.

Each week of the history gets a signal vector (volume, reply-time median, late-night
share, vocabulary drift, affection share, timezone gap between the two people). The
vectors are z-scored and split by an exact optimal segmentation (least squares, the
objective PELT optimizes) with a minimum era length and a maximum era count; the number
of eras is chosen with a BIC-style penalty. Naming eras is the agent's job: it reads
them through sample_era().
"""

from __future__ import annotations

import math
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Literal

from sincehello import config
from sincehello.analysis import text as txt
from sincehello.analysis.dataset import ME, Dataset
from sincehello.store import Store
from sincehello.store.store import ME_PERSON_ID

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS

SIGNALS = ("volume", "reply_median", "late_night", "vocab_drift", "affection", "tz_gap")
MAX_REPLY_MS = 6 * HOUR_MS  # same reply definition as the reply_times stat
LATE_NIGHT_END_HOUR = 4  # 00:00-03:59 sender-local, same as the late_night stat
DRIFT_WINDOW_WEEKS = 4  # vocabulary drift compares a week with the previous four
VOCAB_DIMS = 30  # weekly shares of the top content words, so topic shifts can split eras
PENALTY_PER_DIM = 0.7  # x log(weeks) per live dimension per extra era (tuning: SPEC §7.2)
NOISE_FLOOR = 0.1  # a signal's noise scale is at least this share of its spread
THREAD_GAP_MS = HOUR_MS  # sample_era: a gap this long ends a thread
HEARTS = frozenset("❤♥💕💖💗💘💞💓💝😍🥰😘🫶")

Strategy = Literal["random", "spike_days", "longest_threads"]
STRATEGIES: tuple[str, ...] = ("random", "spike_days", "longest_threads")


@dataclass(frozen=True)
class Era:
    id: str  # stable within a run: "era_1", "era_2", ... in time order
    start_utc: datetime  # tz-aware
    end_utc: datetime  # tz-aware, exclusive
    message_count: int
    signals: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_utc": _iso(self.start_utc),
            "end_utc": _iso(self.end_utc),
            "message_count": self.message_count,
            "signals": dict(self.signals),
        }


def detect_eras(
    store: Store,
    person_id: int,
    *,
    min_weeks: int = 6,
    max_eras: int = 8,
    default_tz: str | None = None,
) -> list[Era]:
    """Chapters of your 1:1 history with one person, in time order.

    Histories shorter than two minimum-length eras come back as a single era. Boundaries
    fall on Mondays 00:00 UTC, except the first era's start (the first message) and the
    last era's end (just after the last message).
    """
    if min_weeks < 1 or max_eras < 1:
        raise ValueError("min_weeks and max_eras must be at least 1")
    ds = Dataset(store, person_id, default_tz or config.default_timezone())
    if not ds.messages:
        return []
    weeks = _weekly(ds)
    columns = [weeks.series[name] for name in SIGNALS]
    columns += _word_columns(weeks.words, VOCAB_DIMS)
    matrix, dims = _normalize(columns)
    penalty = PENALTY_PER_DIM * max(dims, 1) * math.log(max(len(matrix), 2))
    cuts = _segment(matrix, min_weeks, max_eras, penalty)

    first, last = ds.messages[0].sent_at_utc, ds.messages[-1].sent_at_utc
    bounds = [first, *(weeks.origin + c * WEEK_MS for c in cuts), last + 1]
    eras: list[Era] = []
    prev_words: Counter | None = None
    for i, (start, end) in enumerate(pairwise(bounds), 1):
        signals, words = _era_signals(ds, start, end, prev_words)
        prev_words = words
        eras.append(
            Era(
                id=f"era_{i}",
                start_utc=_dt(start),
                end_utc=_dt(end),
                message_count=sum(1 for m in ds.messages if start <= m.sent_at_utc < end),
                signals=signals,
            )
        )
    return eras


def sample_era(
    store: Store,
    person_id: int,
    era: Era,
    *,
    n: int = 40,
    strategy: Strategy = "random",
    seed: int = 0,
    default_tz: str | None = None,
) -> list[sqlite3.Row]:
    """Up to n messages from one era (1:1 only, no reactions), in time order.

    - random: a uniform sample (seeded).
    - spike_days: the busiest days (sender-local dates), the densest run of up to n // 3
      messages from each, busiest first.
    - longest_threads: the threads with the most messages (a gap of an hour ends a thread),
      the opening n // 3 messages of each, longest first.

    Rows are message rows plus a `sender` column ("me" / "them").
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r} (known: {', '.join(STRATEGIES)})")
    if n <= 0:
        return []
    ds = Dataset(
        store,
        person_id,
        default_tz or config.default_timezone(),
        _ms(era.start_utc),
        _ms(era.end_utc),
    )
    msgs = ds.messages
    if strategy == "random":
        picked = random.Random(seed).sample(range(len(msgs)), min(n, len(msgs)))
    elif strategy == "spike_days":
        days: dict[str, list[int]] = defaultdict(list)
        for i, local in enumerate(ds.local):
            days[local.date().isoformat()].append(i)
        ranked = sorted(days.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        picked = _take_chunks([idx for _, idx in ranked], n, ds, densest=True)
    else:
        threads: list[list[int]] = []
        for i, m in enumerate(msgs):
            if not threads or m.sent_at_utc - msgs[i - 1].sent_at_utc >= THREAD_GAP_MS:
                threads.append([])
            threads[-1].append(i)
        ranked_threads = sorted(threads, key=lambda t: (-len(t), t[0]))
        picked = _take_chunks(ranked_threads, n, ds, densest=False)
    ids = [msgs[i].row_id for i in sorted(picked)]
    return _rows(store, ids)


# --- weekly signals ----------------------------------------------------------


@dataclass
class _Weeks:
    origin: int  # ms, Monday 00:00 UTC on or before the first message
    series: dict[str, list[float]]
    words: list[Counter]


def _weekly(ds: Dataset) -> _Weeks:
    first, last = ds.messages[0].sent_at_utc, ds.messages[-1].sent_at_utc
    first_day = first - first % DAY_MS
    origin = first_day - _dt(first_day).weekday() * DAY_MS
    n = (last - origin) // WEEK_MS + 1

    count = [0] * n
    late = [0] * n
    loving = [0] * n
    replies: list[list[float]] = [[] for _ in range(n)]
    words: list[Counter] = [Counter() for _ in range(n)]
    affection = txt.affection_terms()
    prev = None
    for m, local, segs in zip(ds.messages, ds.local, ds.segments, strict=True):
        w = (m.sent_at_utc - origin) // WEEK_MS
        count[w] += 1
        late[w] += local.hour < LATE_NIGHT_END_HOUR
        tokens = [t for seg in segs for t in seg]
        loving[w] += bool(affection.intersection(tokens)) or any(c in HEARTS for c in m.text)
        words[w].update(txt.words(segs))
        if prev and prev.sender != m.sender and m.sent_at_utc - prev.sent_at_utc < MAX_REPLY_MS:
            replies[w].append((m.sent_at_utc - prev.sent_at_utc) / 1000)
        prev = m

    def covered(w: int) -> int:  # ms of week w inside the history
        start, end = origin + w * WEEK_MS, origin + (w + 1) * WEEK_MS
        return max(min(end, last + 1) - max(start, first), DAY_MS)

    nan = math.nan
    series = {
        "volume": [math.log1p(count[w] * WEEK_MS / covered(w)) for w in range(n)],
        "reply_median": [math.log1p(statistics.median(r)) if r else nan for r in replies],
        "late_night": [late[w] / count[w] if count[w] else nan for w in range(n)],
        "vocab_drift": [
            _cosine_distance(words[w], sum(words[w - DRIFT_WINDOW_WEEKS : w], Counter()))
            if w >= DRIFT_WINDOW_WEEKS
            else nan
            for w in range(n)
        ],
        "affection": [loving[w] / count[w] if count[w] else nan for w in range(n)],
        "tz_gap": [_tz_gap_hours(ds, origin + w * WEEK_MS + WEEK_MS // 2) for w in range(n)],
    }
    return _Weeks(origin, {k: _fill(v) for k, v in series.items()}, words)


def _word_columns(words: list[Counter], k: int) -> list[list[float]]:
    """Weekly share of each of the k most frequent content words (ties by word)."""
    if k <= 0:
        return []
    total = sum(words, Counter())
    top = [w for w, _ in sorted(total.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
    sizes = [sum(c.values()) for c in words]
    return [
        _fill([c[w] / size if size else math.nan for c, size in zip(words, sizes, strict=True)])
        for w in top
    ]


def _tz_gap_hours(ds: Dataset, ms: int) -> float:
    def offset(person: int) -> float:
        return ds.tz.local(person, ms).utcoffset().total_seconds() / 3600

    return abs(offset(ds.person_id) - offset(ME_PERSON_ID))


def _cosine_distance(a: Counter, b: Counter) -> float:
    if not a or not b:
        return math.nan
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return 1 - dot / norm


def _fill(values: list[float]) -> list[float]:
    """Fill gaps (weeks with no data) with the previous week's value, else the next's."""
    known = [v for v in values if not math.isnan(v)]
    if not known:
        return [0.0] * len(values)
    out, last = [], known[0]
    for v in values:
        last = last if math.isnan(v) else v
        out.append(last)
    return out


def _normalize(columns: list[list[float]]) -> tuple[list[list[float]], int]:
    """Scale each signal by its week-to-week noise; constant signals become zeros.

    Noise is estimated from the mean absolute difference between consecutive weeks, which
    a few level shifts barely move, so a signal with clean steps (a timezone move) stands
    out while a noisy or spiky one stays near unit scale. Returns rows and live dims.
    """
    zs, live = [], 0
    for col in columns:
        sd = statistics.pstdev(col)
        if sd < 1e-9:
            zs.append([0.0] * len(col))
            continue
        live += 1
        diffs = [abs(b - a) for a, b in pairwise(col)]
        # E|x_t - x_t-1| = 2 sigma / sqrt(pi) for independent Gaussian noise.
        noise = statistics.fmean(diffs) * math.sqrt(math.pi) / 2 if diffs else sd
        scale = max(noise, NOISE_FLOOR * sd)
        mean = statistics.fmean(col)
        zs.append([(v - mean) / scale for v in col])
    return [list(row) for row in zip(*zs, strict=True)], live


# --- segmentation ------------------------------------------------------------


def _segment(
    rows: list[list[float]], min_size: int, max_segments: int, penalty: float
) -> list[int]:
    """Start indices of segments 2..K of the least-squares segmentation, K chosen by penalty.

    Exact dynamic programming over every segmentation with segments of >= min_size rows
    and at most max_segments segments: cost = within-segment sum of squared deviations,
    total = cost + penalty * (K - 1). O(n^2 d + K n^2): about 2 s for ten years of weeks.
    """
    n = len(rows)
    k_max = min(max_segments, n // min_size)
    if k_max <= 1:
        return []
    # Prefix sums per row: p1[j] = per-dim sums of rows[:j], p2[j] = sum of squares.
    p1 = [[0.0] * len(rows[0])]
    p2 = [0.0]
    for row in rows:
        p1.append([a + b for a, b in zip(p1[-1], row, strict=True)])
        p2.append(p2[-1] + sum(v * v for v in row))
    cost: dict[tuple[int, int], float] = {}
    for i in range(n - min_size + 1):
        for j in range(i + min_size, n + 1):
            sq = sum((b - a) ** 2 for a, b in zip(p1[i], p1[j], strict=True))
            cost[i, j] = p2[j] - p2[i] - sq / (j - i)

    inf = math.inf
    # best[k][j]: lowest cost of rows[:j] in k segments; back[k][j]: start of the last one.
    best = [[inf] * (n + 1) for _ in range(k_max + 1)]
    back = [[0] * (n + 1) for _ in range(k_max + 1)]
    for j in range(min_size, n + 1):
        best[1][j] = cost[0, j]
    for k in range(2, k_max + 1):
        prev = best[k - 1]
        for j in range(k * min_size, n + 1):
            for i in range((k - 1) * min_size, j - min_size + 1):
                c = prev[i] + cost[i, j]
                if c < best[k][j]:
                    best[k][j], back[k][j] = c, i
    k_best = min(range(1, k_max + 1), key=lambda k: (best[k][n] + penalty * (k - 1), k))
    cuts, j = [], n
    for k in range(k_best, 1, -1):
        j = back[k][j]
        cuts.append(j)
    return sorted(cuts)


# --- era summaries -----------------------------------------------------------


def _era_signals(
    ds: Dataset, start: int, end: int, prev_words: Counter | None
) -> tuple[dict[str, float], Counter]:
    idx = [i for i, m in enumerate(ds.messages) if start <= m.sent_at_utc < end]
    msgs = [ds.messages[i] for i in idx]
    words: Counter = Counter()
    late = loving = 0
    affection = txt.affection_terms()
    for i in idx:
        segs = ds.segments[i]
        words.update(txt.words(segs))
        late += ds.local[i].hour < LATE_NIGHT_END_HOUR
        tokens = [t for seg in segs for t in seg]
        loving += bool(affection.intersection(tokens)) or any(
            c in HEARTS for c in ds.messages[i].text
        )
    replies = [
        (b.sent_at_utc - a.sent_at_utc) / 1000
        for a, b in pairwise(msgs)
        if a.sender != b.sender and b.sent_at_utc - a.sent_at_utc < MAX_REPLY_MS
    ]
    weeks = (end - start) / WEEK_MS
    total = len(ds.messages)
    span_weeks = max((ds.messages[-1].sent_at_utc + 1 - ds.messages[0].sent_at_utc) / WEEK_MS, 1)
    gaps = [_tz_gap_hours(ds, ms) for ms in range(start + DAY_MS // 2, end, DAY_MS)] or [0.0]
    signals: dict[str, float] = {
        "weeks": round(weeks, 2),
        "messages_per_week": round(len(msgs) / max(weeks, 1 / 7), 2),
        "volume_vs_overall": round((len(msgs) / max(weeks, 1 / 7)) / (total / span_weeks), 3),
        "share_me": round(sum(m.sender == ME for m in msgs) / len(msgs), 4) if msgs else 0.0,
        "late_night_share": round(late / len(msgs), 4) if msgs else 0.0,
        "affection_share": round(loving / len(msgs), 4) if msgs else 0.0,
        "tz_gap_hours": round(statistics.median(gaps), 2),
    }
    if replies:
        signals["reply_median_s"] = round(statistics.median(replies), 1)
    if prev_words is not None and words:
        signals["vocab_shift"] = round(_cosine_distance(words, prev_words), 4)
    return signals, words


# --- sampling helpers --------------------------------------------------------


def _take_chunks(groups: list[list[int]], n: int, ds: Dataset, *, densest: bool) -> list[int]:
    """Up to n // 3 consecutive messages (at least 1) from each group in order, until n."""
    per_group = max(n // 3, 1)
    picked: list[int] = []
    for group in groups:
        k = min(len(group), per_group, n - len(picked))
        if k <= 0:
            break
        start = 0
        if densest and k < len(group):
            times = [ds.messages[i].sent_at_utc for i in group]
            start = min(range(len(group) - k + 1), key=lambda s: (times[s + k - 1] - times[s], s))
        picked.extend(group[start : start + k])
    return picked


def _rows(store: Store, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return store.conn.execute(
        f"""
        SELECT m.*, CASE WHEN m.is_from_me THEN 'me' ELSE 'them' END AS sender
          FROM message m WHERE m.id IN ({marks})
         ORDER BY m.sent_at_utc, m.id
        """,
        ids,
    ).fetchall()


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("era bounds must be timezone-aware")
    return round(dt.timestamp() * 1000)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
