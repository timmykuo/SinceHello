"""Recall of planted timezone periods by timezone inference (SPEC.md §6.1, §10.1).

The manifest's `timezones` are the truth. A period away from the person's first zone by
>= 3h for >= 7 days is expected; a shorter one is a trap that must not be suggested. A
suggestion finds an expected period when it is for the same person, both boundaries are
within `BOUNDARY_DAYS` (or both are open), and its offset is within 1h of the true one
somewhere in the period (daylight saving moves it). Free: no API calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sincehello.analysis.tz_infer import MIN_DAYS, MIN_SHIFT_H, Suggestion, detect
from sincehello.connectors.synthetic import SyntheticConnector
from sincehello.store import Store
from sincehello.store.store import ME_PERSON_ID

BOUNDARY_DAYS = 14  # same tolerance as era boundaries (SPEC §7.2)
OFFSET_TOLERANCE_H = 1.0
CONFIDENT = ("medium", "high")  # false positives only count at these levels


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _offset(tz: str, at: datetime) -> float:
    off = at.astimezone(ZoneInfo(tz)).utcoffset()
    return off.total_seconds() / 3600 if off else 0.0


def planted(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(expected periods, traps) from the manifest's true timezones."""
    expected, traps = [], []
    for who, periods in manifest["timezones"].items():
        home = periods[0]["tz"]
        for p in periods[1:]:
            start = _utc(p["start_utc"])
            end = _utc(p["end_utc"]) if p["end_utc"] else None
            probe = start + ((end - start) / 2 if end else timedelta(days=7))
            if abs(_offset(p["tz"], probe) - _offset(home, probe)) < MIN_SHIFT_H:
                continue  # back home, or too close to count
            row = {"who": who, "tz": p["tz"], "start": start, "end": end}
            if end is not None and end - start < timedelta(days=MIN_DAYS):
                traps.append(row)
            else:
                expected.append(row)
    return expected, traps


def _matches(sg: Suggestion, p: dict[str, Any], *, strict: bool) -> bool:
    start = datetime.fromtimestamp(sg.start_utc / 1000, UTC)
    end = datetime.fromtimestamp(sg.end_utc / 1000, UTC) if sg.end_utc is not None else None
    if not strict:  # overlap is enough
        far = datetime.max.replace(tzinfo=UTC)
        return start < (p["end"] or far) and (end or far) > p["start"]
    tol = timedelta(days=BOUNDARY_DAYS)
    if abs(start - p["start"]) > tol:
        return False
    if (end is None) != (p["end"] is None) or (end and abs(end - p["end"]) > tol):
        return False
    probes = [p["start"] + timedelta(days=1), (p["end"] or p["start"]) - timedelta(days=1)]
    return any(abs(_offset(p["tz"], t) - sg.utc_offset_hours) <= OFFSET_TOLERANCE_H for t in probes)


def score(manifest: dict[str, Any], suggestions: list[Suggestion]) -> dict[str, Any]:
    """Recall of expected periods, trap hits, and confident suggestions matching nothing.

    `suggestions` are `tz_infer.detect` output for the sample's "them" (which includes
    yours).
    """
    expected, traps = planted(manifest)

    def who(sg: Suggestion) -> str:
        return "me" if sg.person_id == ME_PERSON_ID else "them"

    def mine(p: dict[str, Any]) -> list[Suggestion]:
        return [sg for sg in suggestions if who(sg) == p["who"]]

    rows = []
    used: set[int] = set()
    for p in expected:
        hit = next((sg for sg in mine(p) if _matches(sg, p, strict=True)), None)
        if hit is not None:
            used.add(id(hit))
        rows.append(
            {
                "who": p["who"],
                "tz": p["tz"],
                "start": p["start"].isoformat(),
                "end": p["end"].isoformat() if p["end"] else None,
                "found": hit is not None,
                "start_error_days": _err(hit.start_utc, p["start"]) if hit else None,
                "end_error_days": (
                    _err(hit.end_utc, p["end"]) if hit and hit.end_utc and p["end"] else None
                ),
                "offset": hit.utc_offset_hours if hit else None,
                "confidence": hit.confidence if hit else None,
            }
        )
    trap_hits = [
        {"who": p["who"], "tz": p["tz"], "start": p["start"].isoformat()}
        for p in traps
        if any(_matches(sg, p, strict=False) for sg in mine(p))
    ]
    false_positives = [
        sg.to_dict()
        for sg in suggestions
        if id(sg) not in used
        and sg.confidence in CONFIDENT
        and not any(_matches(sg, p, strict=False) for p in expected if p["who"] == who(sg))
    ]
    found = sum(r["found"] for r in rows)
    return {
        "expected": rows,
        "found": found,
        "total": len(rows),
        "recall": found / len(rows) if rows else None,
        "trap_hits": trap_hits,
        "false_positives": false_positives,
    }


def _err(ms: int, truth: datetime) -> float:
    return round((ms / 1000 - truth.timestamp()) / 86400, 1)


def run(path: Path) -> dict[str, Any]:
    """Load a history without its true timezones, infer them, and score the result.

    The default timezone is "me"'s first zone, the stand-in for the Mac's timezone.
    """
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    with Store(":memory:") as store:
        SyntheticConnector(path, load_timezones=False).ingest(store)
        them = store.person_for_handle("synthetic", manifest["participants"]["them"]["handle"])
        found = detect(store, them, default_tz=manifest["timezones"]["me"][0]["tz"])
    out = score(manifest, found)
    out["history_id"] = manifest["history_id"]
    out["suggestions"] = [sg.to_dict() for sg in found]
    return out
