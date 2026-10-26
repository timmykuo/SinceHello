"""Human ratings of a run's insights (SPEC.md §10.3).

`sincehello eval rate <run-id>` asks for the same five 1-5 scores the judge gives plus
👍/👎 on "would I want to see this?", and saves after every card to
`<run_dir>/ratings.json`, i.e. under `~/.sincehello/runs/`, never in the repo: the
ratings describe real conversations. Rating is resumable; rated cards are skipped unless
asked to redo them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.judge.judge import DIMENSIONS
from sincehello.report.schema import Insight, Report

FILE = "ratings.json"


class Rating(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scores: dict[str, int]
    keep: bool
    note: str = ""
    rated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


def path(run_dir: Path) -> Path:
    return run_dir / FILE


def load(run_dir: Path) -> dict[str, Rating]:
    p = path(run_dir)
    if not p.is_file():
        return {}
    raw = json.loads(p.read_text())
    return {k: Rating.model_validate(v) for k, v in raw.get("ratings", {}).items()}


def save(run_dir: Path, ratings: dict[str, Rating]) -> None:
    out = {
        "run_id": run_dir.name,
        "dimensions": list(DIMENSIONS),
        "ratings": {k: r.model_dump() for k, r in ratings.items()},
    }
    tmp = path(run_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    tmp.replace(path(run_dir))


def to_rate(report: Report, everything: bool = False) -> list[Insight]:
    """The deck in order once curation fills it; before that (or with `everything`),
    every insight in the report."""
    if report.deck and not everything:
        by_id = {i.id: i for i in report.insights}
        return [by_id[i] for i in report.deck if i in by_id]
    return list(report.insights)


def parse_scores(text: str) -> dict[str, int]:
    """Five 1-5 scores in `DIMENSIONS` order: "5 4 4 5 3", "5,4,4,5,3" or "54453"."""
    s = text.replace(",", " ").split()
    digits = list(s[0]) if len(s) == 1 else s
    if len(digits) != len(DIMENSIONS) or not all(d in "12345" and len(d) == 1 for d in digits):
        raise ValueError(f"give {len(DIMENSIONS)} scores from 1 to 5, e.g. 5 4 4 5 3")
    return dict(zip(DIMENSIONS, map(int, digits), strict=True))


def as_rows(ratings: dict[str, Rating]) -> dict[str, dict[str, Any]]:
    return {k: {"scores": r.scores, "keep": r.keep} for k, r in ratings.items()}
