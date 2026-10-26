"""Reading a finished run back for evals (SPEC.md §10.4).

A run lives in `~/.sincehello/runs/<run_id>/` (`report.json`, `run_log.jsonl`, and from
the eval commands `eval.json`, `judge.json`, `ratings.json`). The helpers here find a
run, rebuild what the judge needs after the fact (the person's messages and the queries
behind each metric), and summarize a run for `sincehello eval compare`.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sincehello import config
from sincehello.agent.context import ToolContext
from sincehello.agent.runlog import runs_dir
from sincehello.report.schema import Report
from sincehello.store import Store

QUERY_TOOLS = {"count": "count", "first_occurrence": "first_occurrence"}
EVAL_MARK = "-eval-"  # `eval agent` names its runs <run_id>-eval-<sample>


class RunError(Exception):
    pass


def resolve(run: str) -> Path:
    """A run id under the runs directory, or a path to a run directory."""
    for p in (Path(run), runs_dir() / run):
        if (p / "report.json").is_file():
            return p
    raise RunError(f"no run {run!r}: expected report.json in {runs_dir() / run}")


def load_report(run_dir: Path) -> Report:
    return Report.model_validate_json((run_dir / "report.json").read_text())


def read_json(run_dir: Path, name: str) -> dict[str, Any] | None:
    p = run_dir / name
    return json.loads(p.read_text()) if p.is_file() else None


def sample_of(run_dir: Path) -> str | None:
    name = run_dir.name
    return name.split(EVAL_MARK, 1)[1] if EVAL_MARK in name else None


def queries(run_dir: Path, report: Report) -> dict[str, dict[str, Any]]:
    """query_id -> {kind, args} for every query the run's metrics could cite.

    Stats come from `report.stats`; counts and first occurrences from the run log's
    `tool` events (their input is the query's arguments).
    """
    out = {
        qid: {"kind": "stat", "args": {"stat": s["stat"], "params": s["params"]}}
        for qid, s in report.stats.items()
    }
    log = run_dir / "run_log.jsonl"
    if not log.is_file():
        return out
    with log.open(encoding="utf-8") as f:
        for line in f:
            event = json.loads(line)
            kind = QUERY_TOOLS.get(event.get("name", ""))
            if event.get("event") != "tool" or kind is None or event.get("is_error"):
                continue
            try:
                qid = json.loads(event["result"])["query_id"]
            except (ValueError, KeyError, TypeError):
                continue
            out[qid] = {"kind": kind, "args": event.get("input")}
    return out


@contextmanager
def context(run_dir: Path, report: Report, sample: str | None = None) -> Iterator[ToolContext]:
    """A ToolContext over the run's messages.

    Eval runs on a synthetic sample (`--sample`, or the `-eval-<sample>` suffix) re-ingest
    the sample into a throwaway store, since `eval agent` deletes its store. Other runs
    read the local store (`~/.sincehello/sincehello.db`).
    """
    sample = sample or sample_of(run_dir)
    if sample is None:
        with Store(config.store_path()) as store:
            pid = report.person.get("id")
            if pid not in {p.person_id for p in store.people()}:
                raise RunError(f"person {pid} from {run_dir.name} is not in the local store")
            yield ToolContext(store, pid)
        return

    from evals import generator
    from sincehello.connectors.synthetic import SAMPLE_DIR, SyntheticConnector

    path = SAMPLE_DIR.parent / sample
    if not (path / "manifest.json").is_file():
        raise RunError(f"no sample {sample!r} in {SAMPLE_DIR.parent}")
    _, manifest = generator.read(path)
    with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / "judge.db") as store:
        SyntheticConnector(path).ingest(store)
        pid = store.person_for_handle("synthetic", manifest["participants"]["them"]["handle"])
        yield ToolContext(store, pid)


def deck(report: Report) -> list[Any]:
    """What a reader would see: the curated deck, or before curation (M6) every insight
    the critic passed."""
    if report.deck:
        by_id = {i.id: i for i in report.insights}
        return [by_id[i] for i in report.deck if i in by_id]
    return [i for i in report.insights if i.verification.critic == "pass"]


def summary(run_dir: Path) -> dict[str, Any]:
    """One run's numbers for `eval compare`: recall, judge scores, ratings, cost."""
    report = load_report(run_dir)
    run = report.run
    out: dict[str, Any] = {
        "run_id": run.run_id,
        "status": run.status,
        "model": run.model,
        "effort": run.effort,
        "cost_usd": run.cost_usd,
        "turns": run.turns,
        "insights": len(report.insights),
        "critic_pass": sum(i.verification.critic == "pass" for i in report.insights),
        "deck": len(deck(report)),
    }
    if ev := read_json(run_dir, "eval.json"):
        out["recall"] = ev.get("recall")
    if jd := read_json(run_dir, "judge.json"):
        out["judge"] = {
            "overall": (jd.get("means") or {}).get("overall"),
            "keep_rate": jd.get("keep_rate"),
            "hallucination_rate": jd.get("hallucination_rate"),
            "prompt_version": jd.get("judge", {}).get("prompt_version"),
        }
    if rt := read_json(run_dir, "ratings.json"):
        rated = list(rt.get("ratings", {}).values())
        out["human"] = {
            "rated": len(rated),
            "keep_rate": round(sum(r["keep"] for r in rated) / len(rated), 3) if rated else None,
        }
    return out


@contextmanager
def event_log(path: Path) -> Iterator[Any]:
    """A `log(kind, **data)` that appends JSON lines to `path`, like the run log."""
    from datetime import UTC, datetime

    from sincehello.agent.runlog import as_dict

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:

        def log(kind: str, **data: Any) -> None:
            line = {"event": kind, "at": datetime.now(UTC).isoformat(timespec="milliseconds")}
            line.update(as_dict(data))
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            f.flush()

        yield log
