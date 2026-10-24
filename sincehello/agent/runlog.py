"""Run output under ~/.sincehello/runs/<run_id>/ (SPEC.md §5, §8.1, §12).

- `run_log.jsonl`: one JSON event per line. The first `request_static` event holds the
  model, system prompt, tools and parameters; each `request` event holds only the
  messages appended since the previous request, so the full request of any turn is
  `static` + every `new_messages` so far. `response` events carry the whole response,
  usage, and cost (turn and cumulative). `tool` events record each call and outcome.
- `report.json`: the draft report (insights unranked; curation is M6).
- `egress.json`: which message ids went to Claude, and what share of the history.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sincehello import __version__, config
from sincehello.agent.context import ToolContext, iso
from sincehello.report.schema import Report, RunInfo


def new_run_id(person_id: int) -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-p{person_id}-{secrets.token_hex(2)}"


def runs_dir() -> Path:
    return config.home() / "runs"


def as_dict(obj: Any) -> Any:
    """SDK objects to plain JSON-able dicts (API field names), recursively."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(obj, dict):
        return {k: as_dict(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [as_dict(v) for v in obj]
    return obj


class RunRecorder:
    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_dir.name
        self._log = (self.dir / "run_log.jsonl").open("a", encoding="utf-8")

    def event(self, kind: str, **data: Any) -> None:
        line = {"event": kind, "at": datetime.now(UTC).isoformat(timespec="milliseconds")}
        line.update(as_dict(data))
        self._log.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self._log.flush()

    def close(self) -> None:
        self._log.close()

    def write_outputs(
        self,
        ctx: ToolContext,
        *,
        status: str,
        model: str,
        effort: str,
        cost: float,
        tokens: dict[str, int],
        turns: int,
    ) -> Report:
        egress = ctx.egress.summary(len(ctx.messages))
        (self.dir / "egress.json").write_text(
            json.dumps({**egress, "entries": ctx.egress.entries}, indent=2, ensure_ascii=False)
        )
        stats = {
            qid: {"stat": q["args"]["stat"], "params": q["args"]["params"], "value": q["value"]}
            for qid, q in ctx.queries.items()
            if q["kind"] == "stat"
        }
        eras = ctx.eras if isinstance(ctx.eras, list) else []
        report = Report(
            person={"id": ctx.person_id, "name": ctx.person_name},
            range={
                "first_utc": iso(ctx.messages[0].sent_at_utc) if ctx.messages else None,
                "last_utc": iso(ctx.messages[-1].sent_at_utc) if ctx.messages else None,
            },
            sources=sorted({m.id.split(":", 1)[0] for m in ctx.messages}),
            deck=[],
            insights=ctx.insights,
            eras=[e.to_dict() for e in eras],
            stats=stats,
            summary=ctx.summary,
            run=RunInfo(
                run_id=self.run_id,
                status=status,
                cost_usd=round(cost, 6),
                tokens=tokens,
                turns=turns,
                messages_shared=egress["messages_shared"],
                pct_shared=egress["pct_shared"],
                model=model,
                effort=effort,
                version=__version__,
            ),
        )
        (self.dir / "report.json").write_text(report.model_dump_json(indent=2))
        return report
