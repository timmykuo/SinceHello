"""Prompt text for the discovery agent. `system.md` must stay byte-stable across runs
(it is the cached prefix); run-specific text goes in the other templates."""

from __future__ import annotations

from functools import cache
from importlib import resources


@cache
def load(name: str) -> str:
    return resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
