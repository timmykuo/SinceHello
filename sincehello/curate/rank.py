"""Curation (SPEC.md §7.7): turn a run's verified insights into the ordered deck.

Deterministic and offline: it reads only the `Report`, so a run directory can be
re-curated later (`sincehello curate <run_dir>`) with the same result every time.

1. **Eligible:** deterministic checks passed and the critic said `pass`. An insight with
   critic `pending` and no critic notes is one no critic ever saw (`--no-critic`); it is
   eligible, and the deck is marked unverified. `pending` *with* notes means the critic
   ran but couldn't give a verdict (refusal, malformed output, budget), and stays out.
2. **Score** = delight × specificity × confidence, each critic score / 5, so 0.008–1.
   Confidence is the critic's accuracy score. A missing score counts as 3 (the rubric's
   midpoint), so without a critic every card scores 0.216 and ties break by id.
3. **Dedupe:** walking from the best score down, a card is dropped as a duplicate of an
   already-kept one when they tell the same storyline (same `first` pattern, same metric
   query and path, or the same era's story card) or, within one category, cite mostly
   the same messages or have mostly the same title and headline words.
4. **Select, spreading categories:** each card already picked from a category multiplies
   the next one's score by `spread`, and no category fills more than `category_cap` of
   the deck (relaxed only when the deck would otherwise stay under `min_cards`). The
   milestone citing the first message ever ("since hello") is always picked when eligible. Past
   `min_cards`, cards scoring under `min_score` are left out; never more than `max_cards`.
5. **Order like Wrapped:** opener (the earliest-dated milestone, "since hello" when it is
   in) → rhythm/stats → language → free discoveries → milestones in date order → eras
   story in era order → closer (the best free discovery, if any).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from math import prod

from sincehello.analysis.text import segments, words
from sincehello.report.schema import Curation, Insight, Report

NEUTRAL_SCORE = 3  # a missing critic score: the middle of the 1–5 rubric
SECTIONS = ("rhythm", "language", "free", "milestone", "story")  # between opener and closer


@dataclass(frozen=True)
class CurationConfig:
    min_cards: int = 12
    max_cards: int = 20
    min_score: float = 0.2  # past min_cards, stop before a card under this (3×3×3 = 0.216)
    spread: float = 0.75  # score multiplier per card already picked from the same category
    category_cap: float = 0.4  # share of max_cards one category may fill (at least 2 cards)

    def __post_init__(self) -> None:
        if not 1 <= self.min_cards <= self.max_cards:
            raise ValueError(
                f"need 1 <= min_cards <= max_cards, got {self.min_cards}..{self.max_cards}"
            )


def eligible(ins: Insight) -> str | None:
    """None when the insight may go in the deck, else why not."""
    v = ins.verification
    if v.deterministic != "pass":
        return f"deterministic checks: {v.deterministic}"
    if v.critic == "pass" or (v.critic == "pending" and not v.critic_notes):
        return None
    return f"critic {v.critic}" + (f" ({v.critic_notes[0]})" if v.critic_notes else "")


def score(ins: Insight) -> float:
    s = ins.scores
    parts = (s.delight, s.specificity, s.accuracy)
    return round(prod((NEUTRAL_SCORE if p is None else p) / 5 for p in parts), 4)


def curate(report: Report, config: CurationConfig | None = None) -> Report:
    """A copy of `report` with `deck` and `curation` filled."""
    config = config or CurationConfig()
    dropped: dict[str, str] = {}
    pool: list[Insight] = []
    for ins in report.insights:
        if reason := eligible(ins):
            dropped[ins.id] = reason
        else:
            pool.append(ins)
    scores = {ins.id: score(ins) for ins in pool}
    pool.sort(key=lambda i: (-scores[i.id], i.id))

    kept: list[Insight] = []
    for ins in pool:
        dup = next((k for k in kept if _duplicate(k, ins)), None)
        if dup:
            dropped[ins.id] = f"duplicate of {dup.id}"
        else:
            kept.append(ins)

    picked = _select(kept, scores, report.range.get("first_utc"), config, dropped)
    deck = _order(picked, scores, report.eras)
    curation = Curation(
        verified=all(i.verification.critic == "pass" for i in picked),
        min_cards=config.min_cards,
        max_cards=config.max_cards,
        scores=scores,
        dropped=dropped,
    )
    return report.model_copy(update={"deck": [i.id for i in deck], "curation": curation})


def _select(
    pool: list[Insight],
    scores: dict[str, float],
    first_utc: str | None,
    config: CurationConfig,
    dropped: dict[str, str],
) -> list[Insight]:
    """Pick cards best-first with a per-category decay and cap. `pool` is best-first."""
    cap = max(2, math.ceil(config.category_cap * config.max_cards))
    pool = list(pool)
    picked: list[Insight] = []
    counts: Counter[str] = Counter()

    def take(ins: Insight) -> None:
        picked.append(ins)
        pool.remove(ins)
        counts[ins.category] += 1

    hello = next(
        (i for i in pool if i.category == "milestone" and first_utc and i.at_utc == first_utc),
        None,
    )
    if hello is not None:
        take(hello)
    while pool and len(picked) < config.max_cards:
        filling = len(picked) < config.min_cards
        options = [i for i in pool if counts[i.category] < cap] or (pool if filling else [])
        if not filling:
            options = [i for i in options if scores[i.id] >= config.min_score]
        if not options:
            break
        take(
            min(options, key=lambda i: (-scores[i.id] * config.spread ** counts[i.category], i.id))
        )

    for ins in pool:
        if len(picked) >= config.max_cards:
            dropped[ins.id] = "deck full"
        elif scores[ins.id] < config.min_score:
            dropped[ins.id] = f"score {scores[ins.id]} under {config.min_score}"
        else:
            dropped[ins.id] = f"category {ins.category} full"
    return picked


def _order(picked: list[Insight], scores: dict[str, float], eras: list[dict]) -> list[Insight]:
    era_rank = {e.get("id"): n for n, e in enumerate(eras)}
    by_score = sorted(picked, key=lambda i: (-scores[i.id], i.id))
    milestones = sorted(
        (i for i in picked if i.category == "milestone"),
        key=lambda i: (i.at_utc is None, i.at_utc or "", -scores[i.id], i.id),
    )
    opener = milestones[0] if milestones else None
    closer = next((i for i in by_score if i.category == "free"), None)
    sections: dict[str, list[Insight]] = {s: [] for s in SECTIONS}
    for ins in milestones if opener is None else milestones[1:]:
        sections["milestone"].append(ins)
    for ins in by_score:
        if ins.category in ("rhythm", "language", "free") and ins is not closer:
            sections[ins.category].append(ins)
    sections["story"] = sorted(
        (i for i in picked if i.category == "story"),
        key=lambda i: (era_rank.get(i.era_id, len(era_rank)), i.at_utc or "~", i.id),
    )
    middle = [ins for s in SECTIONS for ins in sections[s]]
    return [i for i in (opener, *middle, closer) if i is not None]


def _duplicate(a: Insight, b: Insight) -> bool:
    """Whether `b` retells `a` (SPEC.md §7.7: same storyline, or same messages and claim)."""
    if a.first and b.first and _norm(a.first.pattern) == _norm(b.first.pattern):
        return True
    if (
        a.metric
        and b.metric
        and (a.metric.query_id, a.metric.path)
        == (
            b.metric.query_id,
            b.metric.path,
        )
    ):
        return True
    if a.category == b.category == "story" and a.era_id and a.era_id == b.era_id:
        return True
    if a.category != b.category:
        return False
    ea, eb = {e.message_id for e in a.evidence}, {e.message_id for e in b.evidence}
    if 2 * len(ea & eb) >= min(len(ea), len(eb)):
        return True
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) >= 0.5 * len(wa | wb)


def _norm(pattern: str) -> str:
    return " ".join(pattern.lower().split())


def _words(ins: Insight) -> set[str]:
    return set(words(segments(f"{ins.title} {ins.headline}")))
