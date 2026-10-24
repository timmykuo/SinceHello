"""Local paths. All data lives under SINCEHELLO_HOME (default ~/.sincehello)."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("SINCEHELLO_HOME", Path.home() / ".sincehello"))


def store_path() -> Path:
    return home() / "sincehello.db"
