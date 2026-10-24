"""Known connectors. Add new connectors to CONNECTORS."""

from __future__ import annotations

from collections.abc import Callable

from sincehello.connectors.base import Connector

# id -> zero-arg factory. Factories keep imports and setup lazy.
CONNECTORS: dict[str, Callable[[], Connector]] = {}


def get(connector_id: str) -> Connector:
    try:
        return CONNECTORS[connector_id]()
    except KeyError:
        known = ", ".join(sorted(CONNECTORS)) or "none yet"
        raise KeyError(f"unknown connector {connector_id!r} (known: {known})") from None


def all_connectors() -> list[Connector]:
    return [factory() for _, factory in sorted(CONNECTORS.items())]
