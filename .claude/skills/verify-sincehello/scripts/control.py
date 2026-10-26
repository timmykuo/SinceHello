#!/usr/bin/env -S uv run python
"""Drive the `sincehello` CLI in an isolated data dir and keep evidence (see ../SKILL.md).

Run from the repo root: `.claude/skills/verify-sincehello/scripts/control.py <cmd>`.
State lives in `.verify/<run>/` (gitignored): `home/` is a throwaway SINCEHELLO_HOME, and
`evidence/` holds one transcript per driven command and survives cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
)
SAMPLE = ROOT / "evals" / "samples" / "bean-goose"
REAL_HOME = Path.home() / ".sincehello"


def paths(run: str) -> tuple[Path, Path]:
    base = ROOT / ".verify" / run
    return base / "home", base / "evidence"


def env_for(home: Path) -> dict[str, str]:
    # SINCEHELLO_TZ pins the default timezone so outputs match across machines.
    return {**os.environ, "SINCEHELLO_HOME": str(home), "SINCEHELLO_TZ": "America/New_York"}


def record(evidence: Path, label: str, argv: list[str], code: int, out: str, err: str) -> Path:
    evidence.mkdir(parents=True, exist_ok=True)
    n = len(list(evidence.glob("*.txt"))) + 1
    path = evidence / f"{n:02d}-{label}.txt"
    path.write_text(
        f"$ {' '.join(argv)}\nexit: {code}\n--- stdout\n{out}--- stderr\n{err}", encoding="utf-8"
    )
    return path


def run_ids(home: Path) -> set[str]:
    runs = home / "runs"
    return {d.name for d in runs.iterdir()} if runs.is_dir() else set()


def keep_runs(home: Path, transcript: Path, before: set[str]) -> None:
    """Copy agent run dirs this command created next to its transcript, so cleanup keeps them."""
    for run_id in sorted(run_ids(home) - before):
        dest = transcript.with_suffix("") / run_id
        shutil.copytree(home / "runs" / run_id, dest)
        print(f"evidence: {dest}")


def run_cli(run: str, label: str, args: list[str]) -> int:
    home, evidence = paths(run)
    argv = ["uv", "run", "sincehello", *args]
    before = run_ids(home)
    p = subprocess.run(argv, cwd=ROOT, env=env_for(home), capture_output=True, text=True)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    transcript = record(evidence, label, argv, p.returncode, p.stdout, p.stderr)
    print(f"evidence: {transcript}")
    keep_runs(home, transcript, before)
    return p.returncode


def cmd_launch(a: argparse.Namespace) -> int:
    home, _ = paths(a.run)
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    code = run_cli(a.run, "launch-connect", ["connect", "synthetic"])
    return code or cmd_doctor(a)


def cmd_doctor(a: argparse.Namespace) -> int:
    sys.path.insert(0, str(ROOT))
    from sincehello.store.store import SCHEMA_VERSION

    home, evidence = paths(a.run)
    manifest = json.loads((SAMPLE / "manifest.json").read_text())
    rev = subprocess.run(
        ["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    db = home / "sincehello.db"
    problems = []
    if home.resolve() == REAL_HOME.resolve() or ".verify" not in home.parts:
        problems.append(f"home {home} is not a .verify/ scratch dir")
    print(f"checkout: {ROOT}\nrevision: {rev}\nhome: {home}\nevidence: {evidence}")
    if not db.is_file():
        problems.append("no store yet; run `launch`")
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        (messages,) = conn.execute("SELECT count(*) FROM message").fetchone()
        conn.close()
        expected = manifest["stats"]["lines"]
        print(
            f"schema: v{version} (code v{SCHEMA_VERSION})\nmessages: {messages} (sample {expected})"
        )
        if version != SCHEMA_VERSION:
            problems.append("schema version mismatch; relaunch")
        if messages != expected:
            problems.append("message count differs from the sample; relaunch")
    for p in problems:
        print(f"PROBLEM: {p}")
    print("doctor: ok" if not problems else "doctor: not ready")
    return 1 if problems else 0


def cmd_cli(a: argparse.Namespace) -> int:
    return run_cli(a.run, a.name, a.args)


def build_script(steps: list[dict]) -> list:
    sys.path.insert(0, str(ROOT / "tests"))
    from fake_anthropic import critic_reply, message, text, tool_use

    out = []
    for s in steps:
        if "tool" in s:
            out.append(message(tool_use(s["tool"], s.get("input"))))
        elif "critic" in s:
            out.append(critic_reply(s["critic"], s.get("issues")))
        else:
            out.append(message(text(s["text"]), stop_reason="end_turn"))
    return out


def cmd_scripted(a: argparse.Namespace) -> int:
    """Run `analyze` / `eval agent` in-process with the API client swapped for a script."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from fake_anthropic import ScriptedClient
    from typer.testing import CliRunner

    from sincehello import cli

    home, evidence = paths(a.run)
    client = ScriptedClient(build_script(json.loads(Path(a.script).read_text())))
    cli._anthropic_client = lambda: client
    env = {k: v for k, v in env_for(home).items() if k.startswith("SINCEHELLO_")}
    before = run_ids(home)
    result = CliRunner().invoke(cli.app, a.args, env=env)
    out = result.output
    if result.exception and not isinstance(result.exception, SystemExit):
        out += f"exception: {result.exception!r}\n"
    out += f"scripted: {len(client.requests)} requests, {len(client.script)} steps unused\n"
    sys.stdout.write(out)
    argv = ["scripted", a.script, "--", *a.args]
    transcript = record(evidence, a.name, argv, result.exit_code, out, "")
    print(f"evidence: {transcript}")
    keep_runs(home, transcript, before)
    return result.exit_code


def cmd_cleanup(a: argparse.Namespace) -> int:
    home, evidence = paths(a.run)
    if home.exists():
        shutil.rmtree(home)
    print(f"removed {home}\nkept {evidence}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--run", default=ROOT.name, help="Run id; one per checkout (default: checkout dir name)."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("launch", help="Fresh home, ingest the bean-goose sample, then doctor.")
    sub.add_parser("doctor", help="Read-only: is this home isolated, current, and fully loaded?")
    p = sub.add_parser("cli", help="Run `sincehello <args>` against this run's home.")
    p.add_argument("--name", required=True, help="Evidence label, e.g. stats-json.")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p = sub.add_parser("scripted", help="Like cli, with the Claude client replaced by a script.")
    p.add_argument("--name", required=True)
    p.add_argument("--script", required=True, help="JSON list of {tool, input} or {text} steps.")
    p.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("cleanup", help="Delete this run's home; keep its evidence.")
    a = ap.parse_args()
    if getattr(a, "args", None) and a.args[0] == "--":
        a.args = a.args[1:]
    handlers = {
        "launch": cmd_launch,
        "doctor": cmd_doctor,
        "cli": cmd_cli,
        "scripted": cmd_scripted,
        "cleanup": cmd_cleanup,
    }
    return handlers[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
