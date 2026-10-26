"""The LLM critic (verify/critic.py) and its revise loop in propose_insight, offline."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from fake_anthropic import (
    ScriptedClient,
    critic_reply,
    is_critic,
    message,
    refusal,
    text,
    thinking,
    tool_use,
)
from test_agent_loop import ILY_INSIGHT, SID

from sincehello.agent import AgentConfig, run_agent
from sincehello.agent.budget import CRITIC_PRIOR, Budget, response_cost
from sincehello.agent.context import ToolContext
from sincehello.agent.tools import MAX_REVISIONS, build_tools, dispatch
from sincehello.verify.critic import (
    CONTEXT_EACH_SIDE,
    CriticConfig,
    LLMCritic,
    build_request,
    system_prompt,
)

GOOSE = {
    "category": "language",
    "kind": "inside_joke",
    "title": "The goose incident",
    "headline": "Alex named it on April 3, 2021",
    "body": "A goose chased Alex across a parking lot and it never went away.",
    "evidence": [
        {"message_id": SID + "000224", "quote": "a goose just chased me"},
        {"message_id": SID + "000227", "quote": "we are calling this the goose incident"},
    ],
}


@pytest.fixture
def setup(bean_goose):
    """A ToolContext with an LLMCritic over a scripted client, a budget and a log."""
    store, pid = bean_goose
    ctx = ToolContext(store, pid, "UTC")
    tools = {t.name: t for t in build_tools()}
    events: list[tuple[str, dict]] = []
    ctx.budget = Budget(1.0)
    ctx.log = lambda kind, **data: events.append((kind, data))
    client = ScriptedClient([])
    ctx.critic = LLMCritic(client)

    def propose(**insight):
        out = dispatch(tools, ctx, "propose_insight", insight)
        assert out.internal_error is None, out.internal_error
        return out.is_error, (out.content if out.is_error else json.loads(out.content))

    return ctx, client, propose, events


def user_text(request):
    (msg,) = request["messages"]  # its own context: one user message, nothing else
    assert msg["role"] == "user"
    return msg["content"]


def test_request_is_stable_and_cacheable():
    req = build_request(CriticConfig())
    assert req == build_request(CriticConfig())
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"]["effort"] == "medium"
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert list(fmt["schema"]["properties"]) == ["issues", "scores", "verdict"]
    assert req["system"] == [
        {"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}
    ]
    # Structured outputs, not forced tool use; adaptive thinking; refusal fallbacks.
    assert "tools" not in req and "tool_choice" not in req
    assert req["thinking"]["type"] == "adaptive"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    # The rubric names what it must catch, and doesn't penalize sensitivity.
    prompt = system_prompt()
    for must in ("Sarcasm", "love you", "inside joke", "Numbers without a metric", "Never revise"):
        assert must in prompt
    assert "{" not in prompt  # no run-specific template slots in the cached prefix


def test_pass(setup):
    ctx, client, propose, events = setup
    client.script = [critic_reply("pass", delight=5)]
    err, out = propose(**ILY_INSIGHT)
    assert not err and out == {"status": "accepted", "id": "ins_001", "insights_so_far": 1}
    ins = ctx.insights[0]
    assert ins.verification.critic == "pass" and ins.verification.revisions == 0
    assert ins.scores.delight == 5 and ins.scores.accuracy == 5

    body = user_text(client.requests[0])
    insight = json.loads(body.split("<insight>")[1].split("</insight>")[0])
    assert insight["headline"] == ILY_INSIGHT["headline"] and "verification" not in insight
    assert "<metric>none</metric>" in body and "<previous_review>" not in body
    convo = body.split("<conversation>\n")[1].split("\n</conversation>")[0].splitlines()
    shown = [json.loads(line) for line in convo if line != "…"]
    cited = [m["id"] for m in shown if m.get("cited")]
    assert cited == [SID + "000507", SID + "000509"]
    assert {"sender", "local_time", "sent_at_utc", "text"} <= set(shown[0])
    # Kinds of events, in order; static request logged once.
    assert [k for k, _ in events] == [
        "critic_request_static",
        "critic_request",
        "critic_response",
        "critic_verdict",
    ]


def test_metric_is_shown_with_its_query_and_recomputed_value(setup):
    ctx, client, propose, _ = setup
    count = next(t for t in build_tools() if t.name == "count")
    qid = count.func(ctx, pattern=r"\bgoose incident\b")["query_id"]
    client.script = [critic_reply("pass")]
    insight = {**GOOSE, "metric": {"query_id": qid, "path": "total", "value": "32"}}
    err, _ = propose(**insight)
    assert not err
    body = user_text(client.requests[0])
    metric = json.loads(body.split("<metric>")[1].split("</metric>")[0])
    assert metric["query"] == {
        "kind": "count",
        "args": {"pattern": r"\bgoose incident\b", "sender": "any", "group_by": "none"},
    }
    assert metric["claimed"] == "32" and metric["recomputed"] == 32


def test_context_messages_go_to_the_egress_log(setup):
    ctx, client, propose, _ = setup
    client.script = [critic_reply("pass")]
    before = len(ctx.egress.entries)
    propose(**GOOSE)
    logged = ctx.egress.entries[before:]
    assert {e["tool"] for e in logged} == {"critic"} and all(e["with_text"] for e in logged)
    expected = set()
    for ev in GOOSE["evidence"]:
        expected |= {m.id for m in ctx.around(ev["message_id"], 5, 5)}
    # Overlapping windows are merged: each message is shown (and logged) once.
    assert [e["message_id"] for e in logged] == sorted(expected, key=ctx.by_id.get)
    assert len(logged) == 3 + 2 * CONTEXT_EACH_SIDE + 1  # 000224..000227 overlap
    body = user_text(client.requests[0])
    assert body.count('"id":') == len(logged)


def test_revise_then_pass(setup):
    ctx, client, propose, events = setup
    note = "The headline says Alex but gives no time zone; use their local date."
    client.script = [critic_reply("revise", [note]), critic_reply("pass")]
    err, out = propose(**ILY_INSIGHT)
    assert err and "critic says revise (draft_001, 2 revisions left)" in out
    assert note in out and "revises='draft_001'" in out
    assert ctx.insights == [] and ctx.rejected_proposals == 1

    fixed = {**ILY_INSIGHT, "headline": "Alex said it first, May 14, 2021, 11:48pm their time"}
    err, out = propose(**fixed, revises="draft_001")
    assert not err and out["status"] == "accepted"
    ins = ctx.insights[0]
    assert ins.verification.revisions == 1 and ins.verification.critic == "pass"
    # The second review is a fresh context that carries only the critic's own notes.
    body = user_text(client.requests[1])
    assert "<previous_review>Revision 1." in body and note in body
    assert "their time" in body
    verdicts = [d["verdict"] for k, d in events if k == "critic_verdict"]
    assert verdicts == ["revise", "pass"]


def test_reject_after_two_revisions(setup):
    ctx, client, propose, _ = setup
    client.script = [critic_reply("revise", [f"problem {i}"]) for i in range(3)]
    err, out = propose(**ILY_INSIGHT)
    assert "2 revisions left" in out
    err, out = propose(**ILY_INSIGHT, revises="draft_001")
    assert "1 revision left" in out
    # Without `revises`, the same claim (same kind, shared citation) is still a revision.
    err, out = propose(**ILY_INSIGHT)
    assert err and f"after {MAX_REVISIONS} revisions, so draft_001 is rejected" in out
    assert "Don't resubmit it" in out and "problem 2" in out
    assert ctx.drafts["draft_001"].revisions == 2 and ctx.drafts["draft_001"].status == "rejected"
    # A closed draft is refused before any critic call (the script is empty now).
    for again in ({"revises": "draft_001"}, {}):
        err, out = propose(**ILY_INSIGHT, **again)
        assert err and "draft_001, which is closed (rejected)" in out
    assert len(client.requests) == 3 and ctx.insights == []


def test_reject_closes_the_draft_and_other_insights_proceed(setup):
    ctx, client, propose, _ = setup
    client.script = [
        critic_reply("reject", ["'love your playlist' is not 'I love you'."], accuracy=1),
        critic_reply("pass"),
    ]
    err, out = propose(**ILY_INSIGHT)
    assert err and "critic says reject (draft_001)" in out
    err, out = propose(**GOOSE)  # a different insight gets its own draft
    assert not err and ctx.drafts["draft_002"].status == "accepted"
    err, out = propose(**ILY_INSIGHT, revises="draft_404")
    assert err and "unknown draft" in out


def test_malformed_response_is_retried_once(setup):
    ctx, client, propose, events = setup
    client.script = [
        message(thinking(), text('{"verdict": "pass"'), stop_reason="end_turn"),
        critic_reply("pass"),
    ]
    err, out = propose(**ILY_INSIGHT)
    assert not err and out["status"] == "accepted"
    assert ctx.insights[0].verification.critic == "pass"
    assert len(client.requests) == 2 and client.requests[0] == client.requests[1]
    assert [k for k, _ in events].count("critic_malformed") == 1
    assert ctx.budget.critic_calls == 2  # both attempts are paid for


@pytest.mark.parametrize(
    "bad",
    [
        message(text("not json"), stop_reason="end_turn"),
        critic_reply("revise", []),  # a revise needs issues
        critic_reply("pass", accuracy=9),  # scores are 1-5
        message(text('{"issues": [], "sco'), stop_reason="max_tokens"),
    ],
)
def test_malformed_twice_keeps_the_insight_unverified(setup, bad):
    ctx, client, propose, _ = setup
    client.script = [bad, bad]
    err, out = propose(**ILY_INSIGHT)
    assert not err and out["status"] == "accepted_unverified"
    assert "malformed critic output" in out["note"]
    ins = ctx.insights[0]
    assert ins.verification.critic == "pending" and ins.verification.deterministic == "pass"
    assert ctx.drafts["draft_001"].status == "unverified"


def test_refusal_keeps_the_insight_unverified_without_retrying(setup):
    ctx, client, propose, events = setup
    client.script = [refusal("general_harms")]
    err, out = propose(**ILY_INSIGHT)
    assert not err and out["status"] == "accepted_unverified"
    assert "declined to review it (general_harms)" in out["note"]
    assert len(client.requests) == 1 and ctx.insights[0].verification.critic == "pending"
    assert ("critic_refusal", {"turn": 0, "revision": 0, "category": "general_harms"}) in events


def test_api_error_keeps_the_insight_unverified(setup):
    ctx, client, propose, _ = setup
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client.script = [anthropic.APIConnectionError(request=request)]
    err, out = propose(**ILY_INSIGHT)
    assert not err and "API error (APIConnectionError)" in out["note"]


def test_budget_accounting(setup):
    ctx, client, propose, _ = setup
    budget = ctx.budget
    client.script = [critic_reply("pass")]
    propose(**ILY_INSIGHT)
    cost, _ = response_cost(critic_reply())
    assert budget.critic_calls == 1 and budget.critic_spent == pytest.approx(cost)
    assert budget.spent == pytest.approx(cost)
    assert budget.turns == 0 and budget.last_turn == 0  # agent projection untouched
    assert budget.tokens["output"] == 1_200
    # Out of budget: no call is made, and the insight is kept unverified.
    budget.spent = budget.max_cost - budget.projected_critic() / 2
    err, out = propose(**GOOSE)
    assert not err and "not enough budget" in out["note"] and len(client.requests) == 1


def test_critic_prior_before_any_call():
    b = Budget(1.0)
    assert b.projected_critic() == CRITIC_PRIOR and b.can_afford_critic()
    b.spent = 1.0 - CRITIC_PRIOR / 2
    assert not b.can_afford_critic()


def test_run_with_critic(bean_goose, tmp_path):
    """The loop and the critic share one client and one budget but not one context."""
    store, pid = bean_goose
    client = ScriptedClient(
        [
            message(tool_use("propose_insight", ILY_INSIGHT)),
            critic_reply("revise", ["Say whose local time 11:48pm is."]),
            message(tool_use("propose_insight", {**ILY_INSIGHT, "revises": "draft_001"})),
            critic_reply("pass", delight=5),
            message(tool_use("finish", {"summary": "ok"})),
        ]
    )
    critic = LLMCritic(client)
    result = run_agent(store, pid, client, AgentConfig(), tmp_path / "run", critic=critic)
    assert result.status == "finished" and result.turns == 3
    assert [is_critic(r) for r in client.requests] == [False, True, False, True, False]
    for r in client.requests:
        if is_critic(r):
            assert len(r["messages"]) == 1 and "discovery agent" not in json.dumps(r)
    # The agent saw the critic's notes as a tool error.
    agent_reqs = [r for r in client.requests if not is_critic(r)]
    (res,) = [b for b in agent_reqs[1]["messages"][-1]["content"] if b["type"] == "tool_result"]
    assert res["is_error"] and "whose local time" in res["content"]

    run = result.report.run
    assert run.critic_calls == 2
    critic_cost = 2 * response_cost(critic_reply())[0]
    agent_cost = sum(response_cost(message())[0] for _ in range(3))
    assert run.critic_cost_usd == pytest.approx(critic_cost, abs=1e-6)
    assert run.cost_usd == pytest.approx(critic_cost + agent_cost, abs=1e-6)
    (ins,) = result.report.insights
    assert ins.verification.model_dump() == {
        "deterministic": "pass",
        "critic": "pass",
        "revisions": 1,
        "critic_notes": [],
    }
    events = [json.loads(line) for line in (tmp_path / "run" / "run_log.jsonl").open()]
    kinds = [e["event"] for e in events]
    assert kinds.count("response") == 3 and kinds.count("critic_response") == 2
    assert kinds.count("critic_request_static") == 1
    critic_resp = [e for e in events if e["event"] == "critic_response"]
    assert all(e["cost_usd"] > 0 and e["turn"] in (1, 2) for e in critic_resp)
    end = events[-1]
    assert end["event"] == "end" and end["critic_calls"] == 2
    egress = json.loads((tmp_path / "run" / "egress.json").read_text())
    assert any(e["tool"] == "critic" for e in egress["entries"])


def test_no_critic_leaves_insights_pending(bean_goose, tmp_path):
    store, pid = bean_goose
    client = ScriptedClient(
        [
            message(tool_use("propose_insight", ILY_INSIGHT)),
            message(tool_use("finish", {"summary": "ok"})),
        ]
    )
    result = run_agent(store, pid, client, AgentConfig(), tmp_path / "run")
    assert result.report.insights[0].verification.critic == "pending"
    assert result.report.run.critic_calls == 0
