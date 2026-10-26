"""Connector interface (SPEC.md §4.1).

A connector reads one messaging app's data and writes it into the normalized store.
Connectors produce plain records; the store owns ids, dedupe, and identity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sincehello.store import Store

ConnectorKind = Literal["automatic", "guided"]
StatusState = Literal["ready", "needs_setup", "error"]
# Normalized reaction kinds. iMessage tapbacks map onto the first six.
ReactionKind = Literal[
    "love", "like", "dislike", "laugh", "emphasize", "question", "emoji", "other"
]


@dataclass(frozen=True)
class HandleRecord:
    source: str
    raw_value: str
    display_name: str | None = None


@dataclass(frozen=True)
class ConversationRecord:
    source: str
    source_conv_id: str
    is_group: bool
    member_handles: tuple[str, ...]  # raw_values of the other participants (never "me")


@dataclass(frozen=True)
class MessageRecord:
    source: str
    source_msg_id: str
    source_conv_id: str
    is_from_me: bool
    sent_at_utc: datetime  # must be timezone-aware
    text: str | None
    sender: str | None = None  # raw_value of the sender's handle; None when is_from_me
    is_reaction: bool = False
    has_attachment: bool = False
    edited: bool = False
    # Reactions only (is_reaction=True): the source_msg_id of the message reacted to, which
    # may not be ingested yet. A reaction that was later removed should not be emitted.
    reacts_to: str | None = None
    reaction_kind: ReactionKind | None = None


@dataclass(frozen=True)
class ConnectorStatus:
    state: StatusState
    detail: str = ""


@dataclass
class IngestReport:
    source: str
    messages_seen: int = 0
    messages_added: int = 0
    conversations: int = 0
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class Connector(Protocol):
    id: str
    kind: ConnectorKind

    def check(self) -> ConnectorStatus:
        """Report whether the connector can run, e.g. "needs Full Disk Access"."""
        ...

    def setup_instructions(self) -> list[str]:
        """Steps to show the user when check() is not ready."""
        ...

    def ingest(self, store: Store) -> IngestReport:
        """Write new data into the store. Must be idempotent.

        Use store.get_cursor(self.id) / store.set_cursor(self.id, ...) for incremental sync.
        """
        ...


def write_batch(
    store: Store,
    source: str,
    handles: Iterable[HandleRecord],
    conversations: Iterable[ConversationRecord],
    messages: Iterable[MessageRecord],
) -> IngestReport:
    """Helper for connectors: write records in dependency order and report counts."""
    report = IngestReport(source=source)
    for h in handles:
        store.upsert_handle(h)
    for c in conversations:
        store.upsert_conversation(c)
        report.conversations += 1
    seen, added = store.add_messages(messages)
    report.messages_seen, report.messages_added = seen, added
    store.resolve_identities()
    return report
