from __future__ import annotations

import importlib.machinery
import json
import sys
import types
from dataclasses import dataclass

import pytest
from conftest import FakeConnector, default_messages, msg

from sincehello.agent import tools as tools_mod
from sincehello.agent.context import ToolContext
from sincehello.agent.tools import MAX_CONTEXT, MAX_SEARCH, Verdict, build_tools, dispatch
from sincehello.report.schema import Scores

ILY = r"\b(i love you|love you|ily|love u)\b"
SID = "synthetic:bean-goose-s42-"


@pytest.fixture
def agent(bean_goose):
    store, pid = bean_goose
    ctx = ToolContext(store, pid, "UTC")
    tools = {t.name: t for t in build_tools()}

    def call(tool, /, **args):
        out = dispatch(tools, ctx, tool, args)
        assert out.internal_error is None, out.internal_error
        return out.is_error, (out.content if out.is_error else json.loads(out.content))

    return ctx, call


def ily_insight(**over):
    insight = {
        "category": "milestone",
        "kind": "first_occurrence",
        "title": 'The first "I love you"',
        "headline": "Alex said it first, May 14, 2021, 11:48pm",
        "body": "You replied 41 seconds later.",
        "evidence": [
            {"message_id": SID + "000507", "quote": "i love you"},
            {"message_id": SID + "000509", "quote": "i love you too"},
        ],
        "first": {"pattern": ILY, "sender": "any"},
    }
    insight.update(over)
    return insight


def test_tool_definitions_are_strict_and_stable():
    defs = [t.definition() for t in build_tools()]
    assert json.dumps(defs) == json.dumps([t.definition() for t in build_tools()])
    names = [d["name"] for d in defs]
    assert names[:6] == [
        "get_overview",
        "get_stats",
        "search_messages",
        "first_occurrence",
        "count",
        "get_context",
    ]
    assert names[-2:] == ["propose_insight", "finish"]
    # The real eras/miners modules are present, so their tools are registered.
    assert names[6:8] == ["sample_era", "list_candidates"]

    def objects(schema):
        if schema.get("type") == "object":
            yield schema
            for p in schema["properties"].values():
                yield from objects(p)
        if schema.get("type") == "array":
            yield from objects(schema["items"])

    for d in defs:
        assert d["strict"] is True
        for obj in objects(d["input_schema"]):
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) <= set(obj["properties"])


def test_overview(agent):
    ctx, call = agent
    err, out = call("get_overview")
    assert not err
    assert out["person"]["name"] == "Alex Chen"
    assert out["stats"]["totals"]["value"]["total"] == 2735
    assert "by_month" not in out["stats"]["totals"]["value"]
    assert [e["id"] for e in out["eras"]][:2] == ["era_1", "era_2"]
    qid = out["stats"]["who_texts_first"]["query_id"]
    assert ctx.queries[qid]["kind"] == "stat"
    # The first message id went out without its text.
    assert ctx.egress.summary(len(ctx.messages))["messages_shared"] == 0
    assert any(e["message_id"] == SID + "000001" for e in ctx.egress.entries)


def test_get_stats_params_and_errors(agent):
    _, call = agent
    err, out = call(
        "get_stats", name="who_texts_first", params=[{"name": "gap_hours", "value": 12}]
    )
    assert not err and out["params"]["gap_hours"] == 12
    # Every value arrives as a JSON number; integer parameters get an int back.
    top = [{"name": "top_n", "value": 3.0}]
    err, out = call("get_stats", name="busiest_day", params=top)
    assert not err and out["params"]["top_n"] == 3 and isinstance(out["params"]["top_n"], int)
    err, out = call("get_stats", name="totals", start="2022-01-01")
    assert not err and out["value"]["total"] < 2735 and out["params"]["start_utc"]
    err, out = call("get_stats", name="totals", params=[{"name": "gap_hours", "value": 3}])
    assert err and "does not take gap_hours" in out
    err, out = call("get_stats", name="busiest_day", params=[{**top[0], "value": 1.5}])
    assert err and "must be an integer" in out


def test_search_keyword_regex_and_caps(agent):
    ctx, call = agent
    err, out = call("search_messages", query='"goose incident"', mode="keyword", limit=100)
    assert not err
    assert out["total_matches"] == 32 and out["returned"] == MAX_SEARCH == 25
    assert out["messages"][0]["id"] == SID + "000227"
    err, out = call("search_messages", query=r"goose\s+incident", mode="regex", sender="me")
    assert not err and all(m["sender"] == "me" for m in out["messages"])
    assert out["total_matches"] == 9
    err, out = call("search_messages", query="it's", mode="keyword")
    assert err and "double quotes" in out
    err, out = call("search_messages", query="(", mode="regex")
    assert err and "invalid regex" in out
    # Reactions quoting the phrase and group-chat messages never come back.
    err, out = call("search_messages", query='"i love you"', mode="keyword", limit=25)
    ids = {m["id"] for m in out["messages"]}
    assert SID + "000508" not in ids and SID + "000215" not in ids
    assert ctx.egress.summary(len(ctx.messages))["messages_shared"] > 25


def test_text_is_truncated(store):
    long = "a" * 900
    FakeConnector([*default_messages(), msg(6, "c-alex", False, long)]).ingest(store)
    ctx = ToolContext(store, 2, "UTC")
    tools = {t.name: t for t in build_tools()}
    out = json.loads(
        dispatch(tools, ctx, "search_messages", {"query": "^a+$", "mode": "regex"}).content
    )
    text = out["messages"][0]["text"]
    assert len(text) < 520 and text.endswith("[truncated]")
    # Only Alex's 1:1 messages are in scope: no group message, no reaction.
    assert [m.id for m in ctx.messages] == ["fake:1", "fake:2", "fake:3", "fake:6"]


def test_first_occurrence_and_count(agent):
    ctx, call = agent
    err, out = call("first_occurrence", pattern=ILY)
    assert not err
    assert out["message_id"] == SID + "000507" and out["sender"] == "them"
    assert out["message"]["text"] == "i love you"
    assert out["query_id"] in ctx.queries
    err, out = call("count", pattern=r"\bbean\b", group_by="sender")
    assert out["groups"] == {"me": 53, "them": 17}
    err, out = call("count", pattern=ILY, group_by="sender")
    assert out["groups"] == {"me": 12, "them": 10}


def test_get_context_caps_and_scope(agent):
    _, call = agent
    err, out = call("get_context", message_id=SID + "000507", before=50, after=50)
    assert not err and len(out["messages"]) == 2 * MAX_CONTEXT + 1
    err, out = call("get_context", message_id=SID + "000215")
    assert err and "group chat" in out
    err, out = call("get_context", message_id=SID + "000508")
    assert err and "reaction" in out
    err, out = call("get_context", message_id="synthetic:nope")
    assert err and "no message" in out


def test_propose_insight_accepts_a_valid_first(agent):
    ctx, call = agent
    err, out = call("propose_insight", **ily_insight())
    assert not err and out == {"status": "accepted", "id": "ins_001", "insights_so_far": 1}
    ins = ctx.insights[0]
    assert ins.verification.deterministic == "pass" and ins.verification.critic == "pending"


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"evidence": [{"message_id": SID + "000509", "quote": "i love you too"}]}, "earliest"),
        (
            {"evidence": [{"message_id": SID + "000507", "quote": "I love you!"}]},
            "not in the message text",
        ),
        ({"evidence": [{"message_id": SID + "000215", "quote": "love you"}]}, "group chat"),
        ({"evidence": [{"message_id": SID + "000508", "quote": "i love you"}]}, "reaction"),
        ({"first": None}, "needs `first`"),
        ({"evidence": []}, "evidence"),
        ({"category": "rhythm", "kind": "stat"}, "needs `metric`"),
    ],
)
def test_propose_insight_failures(agent, change, reason):
    ctx, call = agent
    insight = {k: v for k, v in ily_insight(**change).items() if v is not None}
    err, out = call("propose_insight", **insight)
    assert err and reason in out, out
    assert ctx.insights == [] and ctx.rejected_proposals == 1


def test_metric_must_recompute(agent):
    ctx, call = agent
    _, wtf = call("get_stats", name="who_texts_first")
    _, cnt = call("count", pattern=r"\bgoose incident\b")

    def stat_insight(query_id, path, value):
        return {
            "category": "rhythm",
            "kind": "stat",
            "title": "Who texts first",
            "headline": "Alex starts 63% of your conversations",
            "body": "",
            "evidence": [{"message_id": SID + "000001", "quote": "hey!"}],
            "metric": {"query_id": query_id, "path": path, "value": value},
        }

    err, out = call("propose_insight", **stat_insight(wtf["query_id"], "share.them", "0.63"))
    assert err and "is 0.6281, not 0.63" in out
    err, out = call("propose_insight", **stat_insight(wtf["query_id"], "share.nobody", "1"))
    assert err and "not found" in out
    err, out = call("propose_insight", **stat_insight("q_made_up", "", "1"))
    assert err and "unknown query_id" in out
    err, out = call("propose_insight", **stat_insight(wtf["query_id"], "share.them", "0.6281"))
    assert not err
    err, out = call("propose_insight", **stat_insight(cnt["query_id"], "total", "32"))
    assert not err and len(ctx.insights) == 2


def test_critic_hook(agent):
    ctx, call = agent
    ctx.critic = lambda ins, _ctx: Verdict("revise", ["reads as sarcasm"])
    err, out = call("propose_insight", **ily_insight())
    assert err and "critic says revise" in out and "sarcasm" in out
    ctx.critic = lambda ins, _ctx: Verdict("pass", [], Scores(accuracy=5, delight=5))
    err, out = call("propose_insight", **ily_insight())
    assert not err
    assert ctx.insights[0].verification.critic == "pass" and ctx.insights[0].scores.delight == 5


def test_finish(agent):
    ctx, call = agent
    err, out = call("finish", summary="done")
    assert not err and ctx.finished and ctx.summary == "done"


def test_unknown_tool_and_internal_errors(agent, monkeypatch):
    ctx, _ = agent
    tools = {t.name: t for t in build_tools()}
    assert dispatch(tools, ctx, "nope", {}).is_error

    def boom(ctx, **kw):
        raise KeyError("bug")

    tools["count"] = tools_mod.Tool("count", "", {}, boom)
    out = dispatch(tools, ctx, "count", {"pattern": "x"})
    assert out.is_error and "internal error" in out.content and "KeyError" in out.internal_error


# --- era and miner tools, against the contract of branch m2b-eras-miners ------------


@dataclass
class FakeEra:
    id: str
    start_utc: int
    end_utc: int

    def to_dict(self):
        return {"id": self.id, "start_utc": self.start_utc, "end_utc": self.end_utc}


@dataclass
class FakeCandidate:
    """Same shape as analysis.miners.Candidate: evidence is store message.id values."""

    kind: str
    message_ids: tuple[int, ...]
    details: dict

    def to_dict(self):
        return {
            "kind": self.kind,
            "label": "first x",
            "message_ids": list(self.message_ids),
            "score": 1.0,
            "details": self.details,
        }


def _fake_module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def era_modules(monkeypatch):
    calls = {}

    def detect_eras(store, person_id):
        calls["detect"] = calls.get("detect", 0) + 1
        return [FakeEra("era_1", 0, 1_620_000_000_000), FakeEra("era_2", 1_620_000_000_000, 2**62)]

    def sample_era(store, person_id, era, *, n, strategy, seed):
        calls["sample"] = {"n": n, "strategy": strategy, "seed": seed}
        return store.conn.execute(
            "SELECT * FROM message WHERE sent_at_utc >= ? AND sent_at_utc < ?"
            " ORDER BY sent_at_utc LIMIT ?",
            (era.start_utc, era.end_utc, n),
        ).fetchall()

    def mine(store, person_id, kind=None, *, limit=20):
        calls["mine"] = {"kind": kind, "limit": limit}
        (row_id,) = store.conn.execute(
            "SELECT id FROM message WHERE source_msg_id = ?",
            (SID.removeprefix("synthetic:") + "000507",),
        ).fetchone()
        return [FakeCandidate("first", (row_id, 10**9), {"message_id": row_id, "note": "x" * 900})]

    monkeypatch.setitem(
        sys.modules,
        tools_mod.ERAS_MODULE,
        _fake_module(tools_mod.ERAS_MODULE, detect_eras=detect_eras, sample_era=sample_era),
    )
    monkeypatch.setitem(
        sys.modules, tools_mod.MINERS_MODULE, _fake_module(tools_mod.MINERS_MODULE, mine=mine)
    )
    return calls


def test_era_and_candidate_tools(agent, era_modules):
    ctx, call = agent
    names = [t.name for t in build_tools()]
    assert names[6:8] == ["sample_era", "list_candidates"]
    tools = {t.name: t for t in build_tools()}

    def call2(tool, /, **args):
        out = dispatch(tools, ctx, tool, args)
        assert out.internal_error is None, out.internal_error
        return out.is_error, (out.content if out.is_error else json.loads(out.content))

    err, out = call2("get_overview")
    assert [e["id"] for e in out["eras"]] == ["era_1", "era_2"]
    err, out = call2("sample_era", era_id="era_1", n=500, strategy="spike_days")
    assert not err and era_modules["sample"] == {"n": 40, "strategy": "spike_days", "seed": 0}
    # Rows outside the 1:1 scope (group chat, reactions) are dropped before they go out.
    ids = {m["id"] for m in out["messages"]}
    assert ids and ids <= set(ctx.by_id)
    err, out = call2("sample_era", era_id="era_9")
    assert err and "unknown era" in out
    err, out = call2("list_candidates", kind="")
    assert not err and era_modules["mine"] == {"kind": None, "limit": 20}
    cand = out["candidates"][0]
    assert cand["details"]["note"].endswith("[truncated]")
    # Store row ids come back as agent ids; ids outside the run's scope are dropped.
    assert cand["message_ids"] == [SID + "000507"]
    assert cand["details"]["message_id"] == SID + "000507"
    assert any(e["tool"] == "list_candidates" for e in ctx.egress.entries)
    assert era_modules["detect"] == 1  # cached for the run

    err, out = call2("propose_insight", **ily_insight(era_id="era_7"))
    assert err and "era_id 'era_7'" in out
    err, out = call2("propose_insight", **ily_insight(era_id="era_2"))
    assert not err
