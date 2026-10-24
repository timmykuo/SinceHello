from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sincehello.connectors import (
    ConnectorStatus,
    ConversationRecord,
    HandleRecord,
    IngestReport,
    MessageRecord,
    write_batch,
)
from sincehello.store import Store

T0 = datetime(2021, 3, 1, 12, 0, tzinfo=UTC)


class FakeConnector:
    """In-memory connector used to test the store, registry, and CLI."""

    id = "fake"
    kind = "automatic"

    def __init__(self, messages: list[MessageRecord] | None = None) -> None:
        self.messages = messages if messages is not None else default_messages()

    def check(self) -> ConnectorStatus:
        return ConnectorStatus("ready")

    def setup_instructions(self) -> list[str]:
        return []

    def ingest(self, store: Store) -> IngestReport:
        cursor = int(store.get_cursor(self.id) or 0)
        new = [m for m in self.messages if int(m.source_msg_id) > cursor]
        report = write_batch(
            store,
            self.id,
            handles=[
                HandleRecord("fake", "+15550001111", "Alex"),
                HandleRecord("fake", "+15550002222"),
            ],
            conversations=[
                ConversationRecord("fake", "c-alex", False, ("+15550001111",)),
                ConversationRecord("fake", "c-group", True, ("+15550001111", "+15550002222")),
            ],
            messages=new,
        )
        if self.messages:
            store.set_cursor(self.id, str(max(int(m.source_msg_id) for m in self.messages)))
        return report


def msg(
    i: int, conv: str, from_me: bool, text: str, sender: str | None = None, **kw
) -> MessageRecord:
    return MessageRecord(
        source="fake",
        source_msg_id=str(i),
        source_conv_id=conv,
        is_from_me=from_me,
        sent_at_utc=T0 + timedelta(minutes=i),
        text=text,
        sender=None if from_me else (sender or "+15550001111"),
        **kw,
    )


def default_messages() -> list[MessageRecord]:
    return [
        msg(1, "c-alex", False, "hi! it's alex from the party"),
        msg(2, "c-alex", True, "hello!! so glad you texted"),
        msg(3, "c-alex", False, "i love you"),
        msg(
            4,
            "c-alex",
            True,
            'Loved "i love you"',
            is_reaction=True,
            reacts_to="3",
            reaction_kind="love",
        ),
        msg(5, "c-group", False, "group hangout friday?", sender="+15550002222"),
    ]


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "test.db") as s:
        yield s
