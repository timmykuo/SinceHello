"""Naming inferred timezone periods with Claude (SPEC.md §6.1 step 3). Offline only."""

from __future__ import annotations

import json

import anthropic
import pytest
from fake_anthropic import CRITIC_USAGE, ScriptedClient, message, text, thinking
from typer.testing import CliRunner

from sincehello import cli, config
from sincehello.agent.budget import Budget
from sincehello.agent.context import ToolContext
from sincehello.agent.tz_namer import EGRESS_TOOL, NameUnavailable, TzNamer
from sincehello.analysis import tz_infer
from sincehello.connectors.synthetic import SyntheticConnector
from sincehello.store import Store

NY = "America/New_York"


def answer(**over) -> object:
    out = {
        "notes": "Alex says they just landed in Tokyo on the first day.",
        "place": "Tokyo",
        "iana_tz": "Asia/Tokyo",
        "evidence_message_id": "",
        "quote": "just landed in tokyo",
        **over,
    }
    return message(
        thinking("read it"), text(json.dumps(out)), stop_reason="end_turn", usage=CRITIC_USAGE
    )


@pytest.fixture
def tokyo(tmp_path):
    """The committed sample without true timezones, and its one inferred period."""
    with Store(tmp_path / "s.db") as store:
        SyntheticConnector(load_timezones=False).ingest(store)
        person = store.person_for_handle("synthetic", "+12125550142")
        (sg,) = tz_infer.sync(store, person, default_tz=NY)
        yield store, ToolContext(store, person, NY), sg


def test_names_the_place_and_checks_the_quote(tokyo):
    store, ctx, sg = tokyo
    client = ScriptedClient([answer(evidence_message_id=sg.hint_message_id)])
    budget = Budget(0.25)
    got = TzNamer(client, budget)(sg, ctx)
    assert (got.place, got.iana_tz, got.evidence_message_id) == (
        "Tokyo",
        "Asia/Tokyo",
        sg.hint_message_id,
    )
    assert got.quote == "just landed in tokyo" and got.dropped == []
    assert budget.critic_calls == 1 and budget.spent > 0

    (request,) = client.requests
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["output_config"]["format"]["type"] == "json_schema"
    user = request["messages"][0]["content"]
    assert '"inferred_utc_offset": "UTC+09:00"' in user and "just landed in tokyo" in user
    # Every message shown is in the egress log under its own tool name.
    shown = [e for e in ctx.egress.entries if e["tool"] == EGRESS_TOOL]
    assert shown and all(e["with_text"] for e in shown)
    assert sg.hint_message_id in {e["message_id"] for e in shown}


def test_a_zone_that_does_not_fit_the_offset_is_dropped(tokyo):
    _, ctx, sg = tokyo
    got = TzNamer(ScriptedClient([answer(place="Paris", iana_tz="Europe/Paris")]), Budget(1))(
        sg, ctx
    )
    assert got.place is None and got.iana_tz is None
    assert any("doesn't fit" in d for d in got.dropped)


def test_a_made_up_quote_is_dropped(tokyo):
    _, ctx, sg = tokyo
    client = ScriptedClient([answer(evidence_message_id=sg.hint_message_id, quote="hello osaka")])
    got = TzNamer(client, Budget(1))(sg, ctx)
    assert got.iana_tz == "Asia/Tokyo" and got.evidence_message_id is None
    assert any("containing the quote" in d for d in got.dropped)


def test_malformed_twice_or_refused_gives_up(tokyo):
    _, ctx, sg = tokyo
    bad = message(text("not json"), stop_reason="end_turn", usage=CRITIC_USAGE)
    with pytest.raises(NameUnavailable, match="malformed"):
        TzNamer(ScriptedClient([bad, bad]), Budget(1))(sg, ctx)
    refused = message(stop_reason="refusal", usage=CRITIC_USAGE)
    with pytest.raises(NameUnavailable, match="declined"):
        TzNamer(ScriptedClient([refused]), Budget(1))(sg, ctx)
    with pytest.raises(NameUnavailable, match="budget"):
        TzNamer(ScriptedClient([]), Budget(0))(sg, ctx)
    request = anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]
    with pytest.raises(NameUnavailable, match="API error"):
        TzNamer(ScriptedClient([request]), Budget(1))(sg, ctx)


def test_cli_infer_with_name(tmp_path, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path))
    monkeypatch.setenv("SINCEHELLO_TZ", NY)
    with Store(config.store_path()) as store:
        SyntheticConnector(load_timezones=False).ingest(store)
        person = store.person_for_handle("synthetic", "+12125550142")
    client = ScriptedClient([answer()])
    monkeypatch.setattr(cli, "_anthropic_client", lambda: client)
    result = CliRunner().invoke(cli.app, ["tz", "infer", str(person), "--name"])
    assert result.exit_code == 0, result.output
    assert "Suggested zone: Asia/Tokyo (Tokyo)" in result.output
    assert "naming: 1 calls" in result.output
    assert list((tmp_path / "tz").glob("name-*.json"))  # the egress record
    # A named suggestion keeps its zone when detection runs again.
    result = CliRunner().invoke(cli.app, ["tz", "confirm", str(person)], input="c\n")
    assert "Saved: Asia/Tokyo." in result.output
