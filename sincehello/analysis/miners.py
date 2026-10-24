"""Candidate miners (SPEC.md §7.3): cheap local leads for the discovery agent.

Miners don't decide anything; they point at messages worth reading. Every candidate
cites store message ids (`message.id`) and carries a score, higher = more promising, on
a rough 0-1 scale shared by all kinds so mixed lists rank sensibly. Coverage is the
analysis set (1:1 conversations with one person, no reactions), and times of day and
calendar days are the sender's local time. Sensitive moments are included (SPEC §12).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal

from sincehello import config
from sincehello.analysis import text as txt
from sincehello.analysis.dataset import ME, SENDERS, THEM, Dataset, Msg
from sincehello.store import Store

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

Kind = Literal["first", "pet_name", "spike", "conflict"]
KINDS: tuple[str, ...] = ("first", "pet_name", "spike", "conflict")

MAX_REPLY_MS = 6 * HOUR_MS  # a reply comes within this long, as in the reply_times stat


@dataclass(frozen=True)
class Candidate:
    kind: Kind
    label: str  # e.g. 'first "i love you"', 'pet name "bean"'
    message_ids: tuple[int, ...]  # store message.id values, the evidence
    score: float  # higher = more promising
    details: dict[str, Any]  # JSON-serializable

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "message_ids": list(self.message_ids),
            "score": self.score,
            "details": self.details,
        }


def mine(
    store: Store,
    person_id: int,
    kind: str | None = None,
    *,
    limit: int = 20,
    default_tz: str | None = None,
) -> list[Candidate]:
    """Candidates of one kind (or all kinds), best first, at most `limit`."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown candidate kind {kind!r} (known: {', '.join(KINDS)})")
    ds = Dataset(store, person_id, default_tz or config.default_timezone())
    miners = {"first": _firsts, "pet_name": _pet_names, "spike": _spikes, "conflict": _conflicts}
    found: list[Candidate] = []
    for k in KINDS if kind is None else (kind,):
        found.extend(miners[k](ds))
    found.sort(key=lambda c: (-c.score, KINDS.index(c.kind), c.message_ids))
    return found[: max(limit, 0)]


# --- firsts ------------------------------------------------------------------

# (name, pattern, weight). Patterns are matched case-insensitively with re.search over
# message text (curly apostrophes straightened). Weight is the candidate's score.
FIRST_FAMILIES: tuple[tuple[str, str, float], ...] = (
    ("i love you", r"\b(i love you|love you|ily|ilysm|love u|luv you|luv u|love ya)\b", 1.0),
    ("miss you", r"\b(i miss you|miss you|miss u|missing you|miss ya)\b", 0.85),
    ("babe", r"\b(babe|baby|bae|honey|sweetheart|sweetie|darling)\b", 0.8),
    ("goodnight", r"\b(good ?night|gn|nighty? ?night|night night)\b", 0.7),
    ("happy birthday", r"\b(happy (birthday|bday|b-day)|hbd)\b", 0.7),
)


def _firsts(ds: Dataset) -> list[Candidate]:
    out = []
    for name, pattern, weight in FIRST_FAMILIES:
        rx = re.compile(pattern, re.IGNORECASE)
        hits = [i for i, m in enumerate(ds.messages) if rx.search(_plain(m.text))]
        if not hits:
            continue
        first = hits[0]
        m = ds.messages[first]
        by_sender = Counter(ds.messages[i].sender for i in hits)
        first_by = {s: next((i for i in hits if ds.messages[i].sender == s), None) for s in SENDERS}
        reply = _reply_to(ds, first)
        details: dict[str, Any] = {
            "family": name,
            "pattern": pattern,
            "message_id": m.row_id,
            "sender": m.sender,
            "text": m.text,
            "match": rx.search(_plain(m.text)).group(0),
            "sent_at_utc": _iso(m.sent_at_utc),
            "sender_local_time": _local_iso(ds, first),
            "count": len(hits),
            "count_by_sender": {s: by_sender[s] for s in SENDERS},
            "first_by_sender": {s: None if i is None else _ref(ds, i) for s, i in first_by.items()},
            "reply": None,
        }
        ids = [m.row_id]
        if reply is not None:
            r = ds.messages[reply]
            details["reply"] = {
                **_ref(ds, reply),
                "delay_s": (r.sent_at_utc - m.sent_at_utc) // 1000,
                "matches": bool(rx.search(_plain(r.text))),
            }
            ids.append(r.row_id)
        other = first_by[THEM if m.sender == ME else ME]
        if other is not None and ds.messages[other].row_id not in ids:
            ids.append(ds.messages[other].row_id)
        out.append(Candidate("first", f'first "{name}"', tuple(ids), weight, details))
    return out


def _reply_to(ds: Dataset, i: int) -> int | None:
    """The next message, if the other person sent it within MAX_REPLY_MS."""
    m = ds.messages[i]
    for j in range(i + 1, len(ds.messages)):
        nxt = ds.messages[j]
        if nxt.sent_at_utc - m.sent_at_utc >= MAX_REPLY_MS:
            return None
        if nxt.sender != m.sender:
            return j
    return None


# --- pet names ---------------------------------------------------------------

PET_MIN_USES = 5  # messages containing the token, both senders together
PET_MIN_ASYMMETRY = 2.0  # per-message rate of the heavier user / the lighter one
PET_MIN_EDGE_SHARE = 0.5  # share of uses as the first or last word of a message
PET_MIN_SCORE = 0.25
PET_BASELINE_RATIO = 3.0  # must be this much more frequent here than in my other chats


def _pet_names(ds: Dataset) -> list[Candidate]:
    sent = Counter(m.sender for m in ds.messages)
    if not sent[ME] or not sent[THEM]:
        return []
    uses: dict[str, list[int]] = defaultdict(list)
    edge: Counter = Counter()
    neighbors: dict[str, set[str]] = defaultdict(set)
    for i, segs in enumerate(ds.segments):
        tokens = [t for seg in segs for t in seg]
        if not tokens:
            continue
        for t in dict.fromkeys(tokens):
            uses[t].append(i)
        edge.update({tokens[0], tokens[-1]})
        for a, b in pairwise(tokens):
            neighbors[a].add(b)
            neighbors[b].add(a)

    known = txt.pet_name_terms()
    dictionary = txt.common_english() | txt.chat_slang()
    names = _name_tokens(ds)
    baseline = _baseline_rates(ds)
    total_msgs = len(ds.messages)
    out = []
    for token, idx in uses.items():
        n = len(idx)
        if n < PET_MIN_USES or not token.isalpha() or not 2 <= len(token) <= 12:
            continue
        if token in names:
            continue
        if token not in known:
            if {token, txt.squeeze(token), re.sub(r"(.)\1+", r"\1", token)} & dictionary:
                continue
            if baseline.get(token, 0.0) * PET_BASELINE_RATIO >= n / total_msgs:
                continue
        by_sender = Counter(ds.messages[i].sender for i in idx)
        rate = {s: (by_sender[s] + 0.5) / sent[s] for s in SENDERS}
        heavy = max(SENDERS, key=lambda s: (rate[s], s == ME))
        light = THEM if heavy == ME else ME
        asymmetry = rate[heavy] / rate[light]
        edge_share = edge[token] / n
        if asymmetry < PET_MIN_ASYMMETRY or edge_share < PET_MIN_EDGE_SHARE:
            continue
        # Names are attached to many different messages and words; template phrases
        # ("have you eaten") repeat the same message and the same neighbors.
        variety = len({_plain(ds.messages[i].text).lower() for i in idx}) / n
        contexts = len(neighbors[token]) / n
        raw = math.log(asymmetry) * edge_share * variety * contexts * math.log(n)
        if token in known:
            raw *= 1.5
        score = round(1 - math.exp(-raw), 4)
        if score < PET_MIN_SCORE:
            continue
        first = idx[0]
        evidence = [first]
        first_light = next((i for i in idx if ds.messages[i].sender == light), None)
        if first_light is not None:
            evidence.append(first_light)
        step = max(len(idx) // 4, 1)
        evidence.extend(i for i in idx[step::step] if i not in evidence)
        evidence = sorted(evidence[:5])
        details = {
            "term": token,
            "pattern": rf"\b{token}\b",
            "count": n,
            "count_by_sender": {s: by_sender[s] for s in SENDERS},
            "rate_by_sender": {s: round(by_sender[s] / sent[s], 4) for s in SENDERS},
            "main_user": heavy,
            "asymmetry": round(asymmetry, 2),
            "edge_share": round(edge_share, 3),
            "known_term": token in known,
            "first_message_id": ds.messages[first].row_id,
            "first_sender": ds.messages[first].sender,
            "first_at_utc": _iso(ds.messages[first].sent_at_utc),
            "first_by_sender": {
                s: next((_ref(ds, i) for i in idx if ds.messages[i].sender == s), None)
                for s in SENDERS
            },
            "relationship_month": _relationship_month(ds, first),
        }
        out.append(
            Candidate(
                "pet_name",
                f'pet name "{token}"',
                tuple(ds.messages[i].row_id for i in evidence),
                score,
                details,
            )
        )
    return out


def _name_tokens(ds: Dataset) -> set[str]:
    """Lowercased name parts of everyone in the store: names aren't pet names."""
    rows = ds.store.conn.execute(
        "SELECT display_name FROM person UNION SELECT display_name FROM handle"
    )
    return {t for r in rows if r[0] for t in re.findall(r"[a-z]+", r[0].lower())}


def _baseline_rates(ds: Dataset) -> dict[str, float]:
    """Per-message rate of each token in my 1:1 chats with everyone else."""
    texts = ds.baseline_texts()
    counts: Counter = Counter()
    for t in texts:
        counts.update({tok for seg in txt.segments(t) for tok in seg})
    return {tok: c / len(texts) for tok, c in counts.items()} if texts else {}


def _relationship_month(ds: Dataset, i: int) -> int:
    """1-based month of the relationship (calendar months since the first message)."""
    start, at = ds.local[0].date(), ds.local[i].date()
    months = (at.year - start.year) * 12 + at.month - start.month - (at.day < start.day)
    return months + 1


# --- spikes ------------------------------------------------------------------

SPIKE_MIN_Z = 3.0
SPIKE_MIN_MESSAGES = 10
SPIKE_WINDOW_DAYS = 28  # baseline: the days within this many days on each side


def _spikes(ds: Dataset) -> list[Candidate]:
    """Days with volume > 3 sd above the mean of the surrounding eight weeks.

    Days are sender-local dates, as in the busiest_day stat. A local baseline (rather than
    the whole history) keeps busy eras from flagging every day in them.
    """
    if not ds.messages:
        return []
    by_day: dict[date, list[int]] = defaultdict(list)
    for i, local in enumerate(ds.local):
        by_day[local.date()].append(i)
    first, last = min(by_day), max(by_day)
    counts = {first + timedelta(d): 0 for d in range((last - first).days + 1)}
    for d, idx in by_day.items():
        counts[d] = len(idx)
    busiest = max(counts.values())
    out = []
    for day, c in counts.items():
        if c < SPIKE_MIN_MESSAGES:
            continue
        window = [
            counts[day + timedelta(k)]
            for k in range(-SPIKE_WINDOW_DAYS, SPIKE_WINDOW_DAYS + 1)
            if k and first <= day + timedelta(k) <= last
        ]
        if len(window) < 7:
            continue
        mean = statistics.fmean(window)
        sd = max(statistics.pstdev(window), 1.0)
        z = (c - mean) / sd
        if z <= SPIKE_MIN_Z:
            continue
        idx = by_day[day]
        step = max(len(idx) // 9, 1)
        evidence = sorted(dict.fromkeys([idx[0], *idx[step::step]]))[:10]
        by_sender = Counter(ds.messages[i].sender for i in idx)
        details = {
            "date": day.isoformat(),
            "count": c,
            "count_by_sender": {s: by_sender[s] for s in SENDERS},
            "baseline_mean": round(mean, 2),
            "baseline_sd": round(sd, 2),
            "z": round(z, 2),
            "window_days": SPIKE_WINDOW_DAYS,
            "is_busiest_day": c == busiest,
            "first_message_id": ds.messages[idx[0]].row_id,
            "last_message_id": ds.messages[idx[-1]].row_id,
        }
        score = round(min(0.95, 0.5 + (z - SPIKE_MIN_Z) / 20), 4)
        out.append(
            Candidate(
                "spike",
                f"spike on {day.isoformat()} ({c} messages)",
                tuple(ds.messages[i].row_id for i in evidence),
                score,
                details,
            )
        )
    return out


# --- conflicts and makeups ---------------------------------------------------

CONFLICT_MIN_GAP_HOURS = 12.0  # floor for a "long gap"; also >= 1.5x the p95 gap
CONFLICT_LOOKAHEAD = 6  # messages after a gap searched for an apology
CONFLICT_LOOKAHEAD_MS = 6 * HOUR_MS
EXCHANGE_GAP_MS = HOUR_MS  # messages closer than this belong to the same exchange
EXCHANGE_MAX = 20

# Apology strength: the strongest matching tier wins. Logistics ("sorry, running late")
# don't count unless the apology is heavy.
_APOLOGY_HEAVY = re.compile(
    r"\b(i was wrong|forgive me|i overreacted|my fault|i shouldn'?t have|i apologi[sz]e"
    r"|i'?m sorry about|i'?m sorry for|sorry about (yesterday|last night|earlier|before|that))\b"
)
_APOLOGY = re.compile(r"\b(i'?m sorry|i am sorry|so sorry|really sorry|my bad)\b")
_SORRY = re.compile(r"\bsorry\b")
_LOGISTICS = re.compile(
    r"\b(late|min|mins|minutes|traffic|bus|train|missed (your|ur) call|can'?t talk|busy"
    r"|in a meeting|wrong (number|chat|person))\b"
)
_TENSION = re.compile(
    r"\b(you said|you always|you never|whatever|forget it|i need (some )?space"
    r"|leave me alone|i'?m done|are you kidding|what else do you want|why would you"
    r"|that'?s not fair|i don'?t know (right now|anymore)|i'?m (so |really )?"
    r"(upset|mad|angry|hurt|annoyed|pissed)|not okay|don'?t talk to me|unbelievable)\b"
    r"|\?!|!\?"
)


def _conflicts(ds: Dataset) -> list[Candidate]:
    msgs = ds.messages
    if len(msgs) < 2:
        return []
    gaps = [b.sent_at_utc - a.sent_at_utc for a, b in pairwise(msgs)]
    longest = max(gaps)
    p95 = sorted(gaps)[max(0, math.ceil(0.95 * len(gaps)) - 1)]
    threshold = max(CONFLICT_MIN_GAP_HOURS * HOUR_MS, 1.5 * p95)

    # (candidate, first and last message index of the fight it covers)
    found: list[tuple[Candidate, int, int]] = []
    for k, gap in enumerate(gaps):
        if gap < threshold:
            continue
        resume = k + 1
        apology = _find_apology(msgs, resume)
        if apology is None:
            continue
        cand = _conflict(ds, apology, resume, gap, longest, "long_gap")
        found.append((cand, _exchange(msgs, resume)[0], apology[0]))

    # Sharp drops in tone without a long gap: tension, then a real apology within hours.
    for i, m in enumerate(msgs):
        strength = _apology_strength(m.text)
        if strength < 0.6:
            continue
        before = [
            j
            for j in range(max(0, i - EXCHANGE_MAX), i)
            if m.sent_at_utc - msgs[j].sent_at_utc <= CONFLICT_LOOKAHEAD_MS
            and _TENSION.search(_plain(msgs[j].text).lower())
        ]
        if len(before) < 2:
            continue
        biggest = max(range(before[0], i), key=lambda j: (gaps[j], j))
        cand = _conflict(ds, (i, strength), biggest + 1, gaps[biggest], longest, "tone_drop")
        found.append((cand, before[0], i))

    # One candidate per fight: overlapping ones (an "i said i'm sorry" mid-fight) lose to
    # the best-scoring one.
    kept: list[tuple[Candidate, int, int]] = []
    for cand, lo, hi in sorted(found, key=lambda f: (-f[0].score, f[1], f[2])):
        if all(hi < k_lo or lo > k_hi for _, k_lo, k_hi in kept):
            kept.append((cand, lo, hi))
    return [cand for cand, _, _ in kept]


def _exchange(msgs: list[Msg], resume: int) -> list[int]:
    """The exchange before a silence: walk back while messages stay close together."""
    start = resume - 1
    while (
        start > 0
        and resume - start < EXCHANGE_MAX
        and msgs[start].sent_at_utc - msgs[start - 1].sent_at_utc < EXCHANGE_GAP_MS
    ):
        start -= 1
    return list(range(start, resume))


def _find_apology(msgs: list[Msg], resume: int) -> tuple[int, float] | None:
    """The strongest apology among the first messages after a gap (earliest on ties)."""
    best: tuple[int, float] | None = None
    start = msgs[resume].sent_at_utc
    for j in range(resume, min(resume + CONFLICT_LOOKAHEAD, len(msgs))):
        if msgs[j].sent_at_utc - start > CONFLICT_LOOKAHEAD_MS:
            break
        s = _apology_strength(msgs[j].text)
        if s > 0 and (best is None or s > best[1]):
            best = (j, s)
    return best


def _apology_strength(text: str) -> float:
    t = _plain(text).lower()
    if _APOLOGY_HEAVY.search(t):
        return 1.0
    if _LOGISTICS.search(t):
        return 0.0
    if _APOLOGY.search(t):
        return 0.6
    return 0.3 if _SORRY.search(t) else 0.0


def _conflict(
    ds: Dataset,
    apology: tuple[int, float],
    resume: int,
    gap: int,
    longest: int,
    trigger: str,
) -> Candidate:
    msgs = ds.messages
    a, strength = apology
    exchange = _exchange(msgs, resume)
    tense = [j for j in exchange if _TENSION.search(_plain(msgs[j].text).lower())]
    start = tense[0] if tense else None
    reply = _reply_to(ds, a)
    evidence = [j for j in (start, resume - 1, a, reply) if j is not None]
    gap_part = gap / longest if longest else 0.0
    score = 0.45 * gap_part + 0.35 * strength + 0.2 * min(1.0, len(tense) / 2)
    hours = gap / HOUR_MS
    details = {
        "trigger": trigger,
        "silence_hours": round(hours, 2),
        "silence_start_utc": _iso(msgs[resume - 1].sent_at_utc),
        "silence_end_utc": _iso(msgs[resume].sent_at_utc),
        "is_longest_silence": gap == longest,
        "last_before_silence_id": msgs[resume - 1].row_id,
        "fight_start_message_id": None if start is None else msgs[start].row_id,
        "fight_starter": None if start is None else msgs[start].sender,
        "tension_message_ids": [msgs[j].row_id for j in tense],
        "apology_message_id": msgs[a].row_id,
        "apology_sender": msgs[a].sender,
        "apology_text": msgs[a].text,
        "apology_strength": strength,
        "apology_reply": None if reply is None else _ref(ds, reply),
    }
    label = f"conflict: {hours:.0f}h silence, then an apology from {msgs[a].sender}"
    if trigger == "tone_drop":
        label = f"conflict: tense exchange, then an apology from {msgs[a].sender}"
    return Candidate(
        "conflict",
        label,
        tuple(msgs[j].row_id for j in sorted(set(evidence))),
        round(score, 4),
        details,
    )


# --- helpers -----------------------------------------------------------------


def _plain(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def _ref(ds: Dataset, i: int) -> dict[str, Any]:
    m = ds.messages[i]
    return {"message_id": m.row_id, "sender": m.sender, "sent_at_utc": _iso(m.sent_at_utc)}


def _iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _local_iso(ds: Dataset, i: int) -> str:
    return ds.local[i].isoformat(timespec="seconds")
