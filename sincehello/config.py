"""Local paths and defaults. All data lives under SINCEHELLO_HOME (default ~/.sincehello)."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def home() -> Path:
    return Path(os.environ.get("SINCEHELLO_HOME", Path.home() / ".sincehello"))


def store_path() -> Path:
    return home() / "sincehello.db"


def default_timezone() -> str:
    """IANA timezone for people with no tz periods (SPEC.md §6.1).

    SINCEHELLO_TZ wins; otherwise the machine's local timezone, falling back to UTC.
    """
    for candidate in (os.environ.get("SINCEHELLO_TZ"), _system_timezone()):
        if candidate and _valid_tz(candidate):
            return candidate
    return "UTC"


def _system_timezone() -> str | None:
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and _valid_tz(tz):
        return tz
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    try:
        return Path("/etc/timezone").read_text().strip() or None
    except OSError:
        return None


def _valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
