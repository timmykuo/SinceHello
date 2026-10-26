"""The LLM rewrite pass, against a stubbed client. These tests never call the API."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from evals import generator
from evals.generator import llm
from evals.generator import scenario as scenario_mod

SC = scenario_mod.load("bean-goose")


@pytest.fixture(scope="module")
def history():
    return generator.generate(SC, seed=11, target_messages=1500)


def slot_ids(prompt: str) -> list[str]:
    return [line.split(" | ")[0] for line in prompt.splitlines() if " | SLOT | " in line]


class StubClient:
    """Mimics client.beta.messages.create; ``respond`` maps slot ids to output lines."""

    def __init__(self, respond=None, stop_reason="end_turn"):
        self.calls: list[dict] = []
        self.respond = respond or (lambda ids: [{"id": i, "text": f"rewritten {i}"} for i in ids])
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        ids = slot_ids(kwargs["messages"][0]["content"])
        body = json.dumps({"lines": self.respond(ids)})
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=body),
            ],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def test_request_shape(history):
    records, manifest = history
    client = StubClient()
    llm.rewrite(SC, records, manifest, client=client, max_chunks=1)
    (call,) = client.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": llm.OUTPUT_SCHEMA}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["fallbacks"] == "default" and call["betas"] == [llm.FALLBACK_BETA]
    assert "thinking" not in call and "temperature" not in call


def test_chunks_go_era_by_era_and_lock_planted_lines(history):
    records, manifest = history
    chunks = llm.chunks(records, manifest)
    era_ids = [c.era["id"] for c in chunks]
    assert era_ids == sorted(era_ids) and set(era_ids) == {e["id"] for e in manifest["eras"]}
    assert all(len(c.rows) <= llm.CHUNK_LINES for c in chunks)
    prompt = llm.build_prompt(
        SC, manifest, next(c for c in chunks if any(r["fact"] == "goose_incident" for r in c.rows))
    )
    assert 'LOCKED | "we are calling this the goose incident"' in prompt
    assert "Sam Rivera" in prompt and "Reserved terms" in prompt
    for line in prompt.splitlines():
        if " | SLOT | " in line:
            assert "goose" not in line.lower()


def test_rewrite_keeps_planted_facts_exact(history):
    records, manifest = history
    new_records, new_manifest, report = llm.rewrite(SC, records, manifest, client=StubClient())
    assert report.rewritten > 0 and report.rejected == 0
    assert report.chunks == len(llm.chunks(records, manifest))
    assert new_manifest["facts"] == manifest["facts"]
    assert new_manifest["generator"]["kind"] == "llm"
    old = {r["id"]: r for r in records}
    for r in new_records:
        o = old[r["id"]]
        assert (r["ts"], r["sender"], r["conv"], r["fact"]) == (
            o["ts"],
            o["sender"],
            o["conv"],
            o["fact"],
        )
        if r["fact"] is not None:
            assert r["text"] == o["text"]
    assert records[5]["text"] != "rewritten"  # inputs are not mutated


def test_reactions_quote_the_rewritten_text(history):
    records, manifest = history
    new_records, *_ = llm.rewrite(SC, records, manifest, client=StubClient())
    by_id = {r["id"]: r for r in new_records}
    reaction = next(
        r
        for r in new_records
        if r["is_reaction"] and r["fact"] is None and by_id[r["reacts_to"]]["fact"] is None
    )
    assert reaction["text"].endswith(f"“{by_id[reaction['reacts_to']]['text']}”")
    assert re.match(r"^(Loved|Liked|Laughed at|Emphasized) ", reaction["text"])


def test_invalid_lines_fall_back_to_offline_text(history):
    records, manifest = history

    def bad(ids):
        out = [
            {"id": ids[0], "text": "the goose incident again"},
            {"id": ids[1], "text": "ok i love you"},
            {"id": ids[2], "text": "x" * 500},
            {"id": ids[3], "text": "   "},
            {"id": "not-a-slot", "text": "hi"},
            {"id": ids[4], "text": "two\nlines"},
        ]
        return out + [{"id": i, "text": "fine"} for i in ids[5:]]

    new_records, new_manifest, report = llm.rewrite(
        SC, records, manifest, client=StubClient(bad), max_chunks=1
    )
    old = {r["id"]: r for r in records}
    ids = llm.chunks(records, manifest)[0].slot_ids
    new = {r["id"]: r for r in new_records}
    for i in ids[:4]:
        assert new[i]["text"] == old[i]["text"]
    assert new[ids[4]]["text"] == "two lines"
    assert report.rejected == 4
    assert new_manifest["facts"] == manifest["facts"]


def test_refusal_keeps_offline_text(history):
    records, manifest = history
    new_records, _, report = llm.rewrite(
        SC, records, manifest, client=StubClient(stop_reason="refusal"), max_chunks=2
    )
    assert report.refusals == 2 and report.rewritten == 0
    assert [r["text"] for r in new_records] == [r["text"] for r in records]


def test_parse_response_garbage():
    chunk = llm.Chunk(
        {"id": "era_1"}, [{"id": "a", "fact": None, "is_reaction": False, "text": "hi"}]
    )
    report = llm.RewriteReport(model="m")
    assert llm.parse_response("not json", chunk, [], report) == {}
    assert report.rejected == 1
    assert llm.parse_response('{"lines": [{"id": "L1", "text": "yo"}]}', chunk, []) == {"a": "yo"}
