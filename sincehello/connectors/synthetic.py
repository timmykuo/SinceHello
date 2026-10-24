"""Synthetic connector (SPEC.md §10.1): load a generated history into the store.

Reads one directory written by the eval generator (``messages.jsonl`` + ``manifest.json``,
format in ``evals/README.md``) so the full pipeline runs unchanged on fake data with
known planted facts. It reads only the files, so it does not depend on the ``evals``
package. Ground-truth fields (``fact``, ``role``) are never written to the store.

The directory comes from ``SINCEHELLO_SYNTHETIC_DIR``, defaulting to the committed sample
in ``evals/samples/``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sincehello.connectors.base import (
    ConnectorStatus,
    ConversationRecord,
    HandleRecord,
    IngestReport,
    MessageRecord,
    write_batch,
)

if TYPE_CHECKING:
    from sincehello.store import Store

SOURCE = "synthetic"
ENV_DIR = "SINCEHELLO_SYNTHETIC_DIR"
SAMPLE_DIR = Path(__file__).resolve().parents[2] / "evals" / "samples" / "bean-goose"
FORMAT_VERSION = 1


class SyntheticConnector:
    id = SOURCE
    kind = "guided"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get(ENV_DIR) or SAMPLE_DIR)

    def check(self) -> ConnectorStatus:
        for name in ("messages.jsonl", "manifest.json"):
            if not (self.path / name).is_file():
                return ConnectorStatus("needs_setup", f"no {name} in {self.path}")
        try:
            manifest = self._manifest()
        except (OSError, json.JSONDecodeError) as e:
            return ConnectorStatus("error", f"unreadable manifest: {e}")
        if manifest.get("format_version") != FORMAT_VERSION:
            return ConnectorStatus("error", f"unsupported format {manifest.get('format_version')}")
        return ConnectorStatus("ready", f"{manifest['history_id']} ({self.path})")

    def setup_instructions(self) -> list[str]:
        return [
            "Generate a history: sincehello synth generate bean-goose --seed 1 --out DIR",
            f"Point the connector at it: export {ENV_DIR}=DIR",
            "Or leave it unset to use the committed sample in evals/samples/.",
        ]

    def ingest(self, store: Store) -> IngestReport:
        manifest = self._manifest()
        people = manifest["participants"]
        handles = [
            HandleRecord(SOURCE, p["handle"], p["name"]) for key, p in people.items() if key != "me"
        ]
        convs = {
            c["key"]: ConversationRecord(
                SOURCE,
                c["source_conv_id"],
                c["is_group"],
                tuple(people[m]["handle"] for m in c["members"]),
            )
            for c in manifest["conversations"]
        }
        report = write_batch(
            store,
            SOURCE,
            handles,
            convs.values(),
            self._messages(people, convs),
        )
        # Re-ingest is idempotent via UNIQUE(source, source_msg_id); the cursor only
        # records which history was loaded last.
        store.set_cursor(SOURCE, manifest["history_id"])
        return report

    def _manifest(self) -> dict[str, Any]:
        return json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))

    def _messages(
        self, people: dict[str, Any], convs: dict[str, ConversationRecord]
    ) -> Iterator[MessageRecord]:
        with (self.path / "messages.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                from_me = r["sender"] == "me"
                yield MessageRecord(
                    source=SOURCE,
                    source_msg_id=r["id"],
                    source_conv_id=convs[r["conv"]].source_conv_id,
                    is_from_me=from_me,
                    sent_at_utc=datetime.fromisoformat(r["ts"].replace("Z", "+00:00")),
                    text=r["text"],
                    sender=None if from_me else people[r["sender"]]["handle"],
                    is_reaction=r["is_reaction"],
                    has_attachment=r["has_attachment"],
                    edited=r["edited"],
                )
