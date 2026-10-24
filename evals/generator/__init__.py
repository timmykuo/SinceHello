"""Synthetic relationship histories with planted facts (SPEC.md §10.1).

``generate`` is the offline, deterministic entry point; ``evals.generator.llm`` adds an
optional Claude rewrite pass for richer filler text. Output format: ``evals/README.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.generator import manifest as manifest_mod
from evals.generator.offline import Generator, history_id_for
from evals.generator.scenario import Scenario, load

MESSAGES_FILE = "messages.jsonl"
MANIFEST_FILE = "manifest.json"


def generate(
    scenario: Scenario | str, seed: int = 0, target_messages: int | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build one history offline. Returns (message records, manifest)."""
    sc = load(scenario) if isinstance(scenario, str) else scenario
    gen = Generator(sc, seed, target_messages)
    msgs, _ = gen.generate()
    records = [m.record() for m in msgs]
    meta = {"kind": "offline", "target_messages": gen.target}
    man = manifest_mod.compute(sc, records, seed, meta)
    manifest_mod.validate(sc, records, man)
    return records, man


def write(out_dir: Path, records: list[dict[str, Any]], manifest: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / MESSAGES_FILE).open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    (out_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_dir


def read(history_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with (history_dir / MESSAGES_FILE).open(encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    manifest = json.loads((history_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    return records, manifest


__all__ = ["generate", "history_id_for", "load", "read", "write"]
