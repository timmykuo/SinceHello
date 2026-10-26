"""The discovery loop against a scripted client and the committed bean-goose sample."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from fake_anthropic import ScriptedClient, message, refusal, text, thinking, tool_use

from sincehello.agent import AgentConfig, run_agent
from sincehello.agent.loop import FALLBACK_BETA, TASK_BUDGET_BETA, replayable
from sincehello.agent.runlog import as_dict
from sincehello.agent.tools import MAX_SEARCH

ILY = r"\b(i love you|love you|ily|love u)\b"
SID = "synthetic:bean-goose-s42-"

ILY_INSIGHT = {
    "category": "milestone",
    "kind": "first_occurrence",
    "title": 'The first "I love you"',
    "headline": "Alex said it first, May 14, 2021, 11:48pm",
    "body": 'You replied 41 seconds later with "i love you too 🥹".',
    "evidence": [
        {"message_id": SID + "000507", "quote": "i love you"},
        {"message_id": SID + "000509", "quote": "i love you too 🥹"},
    ],
    "first": {"pattern": ILY, "sender": "any"},
}


def goose_insight(query_id, value):
    return {
        "category": "language",
        "kind": "count",
        "title": "The goose incident",
        "headline": "One goose, 32 callbacks",
        "body": "Alex named it; you both kept bringing it back.",
        "evidence": [
            {"message_id": SID + "000227", "quote": "we are calling this the goose incident"}
        ],
        "metric": {"query_id": query_id, "path": "total", "value": value},
    }


def run(bean_goose, tmp_path, script, **config):
    store, pid = bean_goose
    client = ScriptedClient(script)
    result = run_agent(store, pid, client, AgentConfig(**config), tmp_path / "run")
    return result, client


def results_of(request):
    """The tool_result blocks the request's last user message carries."""
    last_user = next(m for m in reversed(request["messages"]) if m["role"] == "user")
    return [b for b in last_user["content"] if b["type"] == "tool_result"]


def count_query_id(request):
    """Find the query_id the count tool returned, as the model would read it."""
    for r in results_of(request):
        body = json.loads(r["content"]) if not r.get("is_error") else {}
        if str(body.get("query_id", "")).startswith("q_count_"):
            return body["query_id"]
    raise AssertionError("no count result")


def test_full_run_with_parallel_calls_and_a_corrected_insight(bean_goose, tmp_path):
    script = [
        message(thinking("orient first"), tool_use("get_overview")),
        # Three independent calls in one turn.
        message(
            thinking("look for firsts and the joke"),
            tool_use("first_occurrence", {"pattern": ILY}),
            tool_use("count", {"pattern": r"\bgoose incident\b", "group_by": "sender"}),
            tool_use("search_messages", {"query": '"goose incident"', "mode": "keyword"}),
        ),
        # Two invalid insights: the reply cited instead of the first "i love you", and a
        # miscounted metric.
        lambda req: message(
            tool_use(
                "propose_insight",
                {
                    **ILY_INSIGHT,
                    "evidence": [{"message_id": SID + "000509", "quote": "i love you too"}],
                },
            ),
            tool_use("propose_insight", goose_insight(count_query_id(req), "31")),
        ),
        # Both corrected.
        lambda req: message(
            text("Fixing both."),
            tool_use("propose_insight", ILY_INSIGHT),
            tool_use("propose_insight", goose_insight(_qid_from_history(req), "32")),
        ),
        message(tool_use("finish", {"summary": "Found the first ily and the goose."})),
    ]
    result, client = run(bean_goose, tmp_path, script)

    assert result.status == "finished" and result.turns == 5
    assert [i.id for i in result.report.insights] == ["ins_001", "ins_002"]
    assert result.report.summary == "Found the first ily and the goose."

    reqs = client.requests
    # Parallel calls: three tool_use blocks answered by ONE user message, in order.
    second = reqs[2]["messages"]
    assert second[-1]["role"] == "user"
    assert [r["tool_use_id"] for r in second[-1]["content"]] == [
        b["id"] for b in second[-2]["content"] if b["type"] == "tool_use"
    ]
    # The invalid proposals came back as is_error results with the reasons.
    errs = results_of(reqs[3])
    assert all(r["is_error"] for r in errs)
    assert "earliest match" in errs[0]["content"] and SID + "000507" in errs[0]["content"]
    assert "is 32, not 31" in errs[1]["content"]
    assert not any(r.get("is_error") for r in results_of(reqs[4]))

    # Every request: same system prompt and tools (cache prefix), and the history only
    # ever grows by appending (thinking blocks stay valid).
    for a, b in zip(reqs, reqs[1:], strict=False):
        assert a["system"] == b["system"] and a["tools"] == b["tools"]
        assert b["messages"][: len(a["messages"])] == a["messages"]
    # Thinking blocks are passed back unchanged, signature included.
    assert reqs[1]["messages"][1]["content"][0]["type"] == "thinking"
    assert reqs[1]["messages"][1]["content"][0]["signature"].startswith("sig")

    # Run output.
    out = result.run_dir
    report = json.loads((out / "report.json").read_text())
    assert report["run"]["status"] == "finished" and report["deck"] == []
    assert report["insights"][0]["verification"]["deterministic"] == "pass"
    assert result.cost_usd > 0
    assert report["run"]["cost_usd"] == pytest.approx(result.cost_usd, abs=1e-6)
    egress = json.loads((out / "egress.json").read_text())
    assert egress["messages_shared"] == report["run"]["messages_shared"] > 0
    assert egress["total_messages"] == 2735
    assert any(e["message_id"] == SID + "000507" and e["with_text"] for e in egress["entries"])
    events = [json.loads(line) for line in (out / "run_log.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "request_static" and kinds[-1] == "end"
    assert kinds.count("response") == 5 and kinds.count("tool") == 9
    responses = [e for e in events if e["event"] == "response"]
    assert responses[-1]["total"] == pytest.approx(result.cost_usd)
    assert responses[0]["response"]["usage"]["cache_read_input_tokens"] == 12_000
    # The run log reconstructs every request: static part + new_messages so far.
    logged = [m for e in events if e["event"] == "request" for m in e["new_messages"]]
    assert logged[: len(reqs[-1]["messages"])] == reqs[-1]["messages"]


def _qid_from_history(req):
    for m in req["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list):
            for r in m["content"]:
                if r.get("type") == "tool_result" and not r.get("is_error"):
                    body = json.loads(r["content"])
                    if str(body.get("query_id", "")).startswith("q_count_"):
                        return body["query_id"]
    raise AssertionError("no count result in history")


def test_request_shape(bean_goose, tmp_path):
    result, client = run(
        bean_goose, tmp_path, [message(tool_use("finish", {"summary": "x"}))], effort="medium"
    )
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert req["output_config"]["effort"] == "medium"
    assert req["output_config"]["task_budget"] == {"type": "tokens", "total": 322_580}
    assert req["tool_choice"] == {"type": "auto"}
    assert req["fallbacks"] == "default"
    assert set(req["betas"]) == {FALLBACK_BETA, TASK_BUDGET_BETA}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] for t in req["tools"])
    # Run-specific text lives in the first user message, never in the system prompt.
    assert "Alex Chen" in req["messages"][0]["content"]
    assert "Alex" not in req["system"][0]["text"]


def test_system_prompt_and_tools_are_byte_stable_across_runs(bean_goose, tmp_path):
    store, pid = bean_goose
    seen = []
    for i, cfg in enumerate([AgentConfig(), AgentConfig(max_turns=7, effort="high")]):
        client = ScriptedClient([message(tool_use("finish", {"summary": "x"}))])
        run_agent(store, pid, client, cfg, tmp_path / f"run{i}")
        req = client.requests[0]
        seen.append(json.dumps([req["tools"], req["system"]], sort_keys=False))
    assert seen[0] == seen[1]


def test_refusal_is_dropped_noted_and_the_run_continues(bean_goose, tmp_path):
    script = [
        message(tool_use("get_overview")),
        refusal("bio"),
        message(tool_use("finish", {"summary": "done"})),
    ]
    result, client = run(bean_goose, tmp_path, script)
    assert result.status == "finished"
    assert result.notes == ["turn 2: refusal (bio)"]
    after = client.requests[2]["messages"]
    # Nothing from the refused turn was appended; a system note follows the tool results.
    assert [m["role"] for m in after] == ["user", "assistant", "user", "system"]
    assert "declined by a safety filter (category: bio)" in after[-1]["content"]
    # The refused turn's tokens still count.
    assert result.report.run.turns == 3 and result.cost_usd > 0


def test_repeated_refusals_stop_the_run(bean_goose, tmp_path):
    script = [message(tool_use("get_overview")), refusal(), refusal(), refusal()]
    result, client = run(bean_goose, tmp_path, script)
    assert result.status == "refused" and len(result.notes) == 3
    # The note is appended once; later retries send the same history.
    assert client.requests[2]["messages"] == client.requests[3]["messages"]
    assert (tmp_path / "run" / "report.json").exists()


def test_max_tokens_with_a_cut_off_tool_call(bean_goose, tmp_path):
    script = [
        message(
            tool_use("count", {"pattern": "goose"}),
            tool_use("search_messages", {"query": "go"}),  # cut off: mode missing
            stop_reason="max_tokens",
        ),
        message(text("Let me summarize instead."), stop_reason="max_tokens"),
        message(tool_use("finish", {"summary": "done"})),
    ]
    result, client = run(bean_goose, tmp_path, script)
    assert result.status == "finished"
    results = results_of(client.requests[1])
    assert len(results) == 2 and all(r["is_error"] for r in results)
    assert "max_tokens" in results[0]["content"]
    last = client.requests[2]["messages"][-1]
    assert last["role"] == "user" and "output limit" in last["content"]


def test_budget_wraps_up_then_stops_cleanly(bean_goose, tmp_path):
    # 625 input + 1000 output tokens = $0.0225 per turn on Opus 5.5.
    usage = {"input_tokens": 625, "output_tokens": 1000}
    script = [
        message(tool_use("get_overview"), usage=usage),
        message(tool_use("propose_insight", ILY_INSIGHT), usage=usage),
        # Never sent: after two turns ($0.045 of $0.05), a third would cross the cap.
        message(tool_use("finish", {"summary": "x"}), usage=usage),
    ]
    result, client = run(bean_goose, tmp_path, script, max_cost=0.05)
    assert result.status == "budget" and len(client.requests) == 2
    assert result.cost_usd == pytest.approx(0.045)
    assert [i.id for i in result.report.insights] == ["ins_001"]  # kept
    report = json.loads((result.run_dir / "report.json").read_text())
    assert report["run"]["status"] == "budget" and len(report["insights"]) == 1
    events = [
        json.loads(line) for line in (result.run_dir / "run_log.jsonl").read_text().splitlines()
    ]
    wrap = [e for e in events if e["event"] == "wrap_up"]
    assert wrap and wrap[0]["reason"] == "cost budget"
    # The wrap-up instruction was appended as a mid-conversation system message.
    final = next(e for e in events if e["event"] == "request" and e["turn"] is None)
    assert final["new_messages"][-1]["role"] == "system"
    assert "wrap up" in final["new_messages"][-1]["content"]


def test_turn_cap_with_wrap_up_message(bean_goose, tmp_path):
    script = [message(tool_use("count", {"pattern": "goose"})) for _ in range(5)]
    result, client = run(bean_goose, tmp_path, script, max_turns=5)
    assert result.status == "turn_cap" and len(client.requests) == 5
    # With 3 turns left, the model was told to wrap up, exactly once.
    systems = [m for m in client.requests[-1]["messages"] if m["role"] == "system"]
    assert len(systems) == 1 and "turn limit" in systems[0]["content"]
    assert client.requests[2]["messages"][-1]["role"] == "system"


def test_stops_after_nudges_when_the_model_never_calls_finish(bean_goose, tmp_path):
    script = [message(text("All done!"), stop_reason="end_turn") for _ in range(3)]
    result, client = run(bean_goose, tmp_path, script)
    assert result.status == "no_finish" and len(client.requests) == 3
    assert "call finish" in client.requests[1]["messages"][-1]["content"]


def test_api_error_stops_and_keeps_findings(bean_goose, tmp_path):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid"))
    script = [message(tool_use("propose_insight", ILY_INSIGHT)), err]
    result, _ = run(bean_goose, tmp_path, script)
    assert result.status == "api_error" and len(result.report.insights) == 1
    assert "APIConnectionError" in result.notes[0]


def test_unexpected_exception_still_writes_the_report(bean_goose, tmp_path):
    script = [message(tool_use("propose_insight", ILY_INSIGHT)), RuntimeError("boom")]
    with pytest.raises(RuntimeError):
        run(bean_goose, tmp_path, script)
    report = json.loads((tmp_path / "run" / "report.json").read_text())
    assert report["run"]["status"] == "error" and len(report["insights"]) == 1


def test_fallback_turn_is_echoed_without_the_declined_models_blocks(bean_goose, tmp_path):
    fallback_usage = {
        "input_tokens": 300,
        "output_tokens": 900,
        "iterations": [
            {
                "type": "message",
                "model": "claude-opus-5-5",
                "input_tokens": 300,
                "output_tokens": 100,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            {
                "type": "fallback_message",
                "model": "claude-opus-5",
                "input_tokens": 300,
                "output_tokens": 900,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        ],
    }
    declined = tool_use("count", {"pattern": "never run"})
    script = [
        message(
            thinking("declined model"),
            declined,
            {
                "type": "fallback",
                "from": {"model": "claude-opus-5-5"},
                "to": {"model": "claude-opus-5"},
                "trigger": {"type": "refusal", "category": "bio"},
            },
            thinking("fallback model"),
            tool_use("finish", {"summary": "done"}),
            model="claude-opus-5",
            usage=fallback_usage,
        ),
    ]
    result, client = run(bean_goose, tmp_path, script)
    assert result.status == "finished"
    # Opus 5.5 attempt + Opus 5 attempt, each at its own price.
    assert result.cost_usd == pytest.approx((300 * 4 + 100 * 20 + 300 * 5 + 900 * 25) / 1e6)
    events = [
        json.loads(line) for line in (result.run_dir / "run_log.jsonl").read_text().splitlines()
    ]
    assert [e["name"] for e in events if e["event"] == "tool"] == ["finish"]


def test_replayable_drops_blocks_before_the_last_fallback():
    m = message(
        thinking("a"),
        text("partial answer"),
        tool_use("count", {"pattern": "x"}),
        {
            "type": "fallback",
            "from": {"model": "claude-opus-5-5"},
            "to": {"model": "claude-opus-5"},
            "trigger": {"type": "refusal", "category": None},
        },
        thinking("b"),
        tool_use("finish", {"summary": "y"}),
    )
    kept = replayable(m.content)
    assert [b["type"] for b in kept] == ["text", "thinking", "tool_use"]
    assert kept[2]["name"] == "finish"
    assert as_dict(m.content[0])["type"] == "thinking"


def _optional_params(schema):
    n = 0
    if schema.get("type") == "object":
        required = set(schema["required"])
        for key, sub in schema["properties"].items():
            n += (key not in required) + _optional_params(sub)
    elif schema.get("type") == "array":
        n += _optional_params(schema["items"])
    return n


def test_strict_tools_fit_the_api_limit_and_get_stats_takes_param_pairs(bean_goose, tmp_path):
    # The first live run died on turn 1: the API rejects strict schemas with more than 24
    # optional parameters in total, and get_stats.params alone had 21.
    script = [
        message(
            tool_use(
                "get_stats",
                {
                    "name": "who_texts_first",
                    "start": "2021-01-01",
                    "params": [{"name": "gap_hours", "value": 12}],
                },
            )
        ),
        message(tool_use("finish", {"summary": "done"})),
    ]
    result, client = run(bean_goose, tmp_path, script)

    assert result.status == "finished"
    tools = client.requests[0]["tools"]
    strict = [t for t in tools if t.get("strict")]
    assert len(strict) == len(tools)
    assert sum(_optional_params(t["input_schema"]) for t in strict) <= 24
    [res] = results_of(client.requests[1])
    assert not res.get("is_error")
    params = json.loads(res["content"])["params"]
    assert params["gap_hours"] == 12 and params["start_utc"]


def test_first_only_gates_first_occurrence_and_escaped_quotes_still_match(bean_goose, tmp_path):
    # Live runs: an era insight carried a junk `first` and was rejected for it, and a
    # quote came back with its non-ASCII written as a literal \\uXXXX escape.
    moment = {
        "category": "milestone",
        "kind": "moment",
        "title": "Said it back",
        "headline": "You said it back within a minute",
        "body": "Right away.",
        "evidence": [{"message_id": SID + "000509", "quote": "i love you too \\ud83e\\udd79"}],
        "first": {"pattern": "x", "sender": "any"},
    }
    script = [
        message(tool_use("propose_insight", moment)),
        message(tool_use("finish", {"summary": "done"})),
    ]
    result, client = run(bean_goose, tmp_path, script)

    assert not any(r.get("is_error") for r in results_of(client.requests[1]))
    [insight] = result.report.insights
    assert insight.first is None


def test_stat_parameters_are_listed_and_errors_say_what_a_stat_takes(bean_goose, tmp_path):
    # Live runs: the agent guessed `top_n` for top_emoji and distinctive_phrases, which
    # called it `k`, and the error didn't say what they took instead.
    script = [
        message(tool_use("get_overview")),
        message(
            tool_use("get_stats", {"name": "top_emoji", "params": [{"name": "top_n", "value": 3}]}),
            tool_use(
                "get_stats",
                {"name": "distinctive_phrases", "params": [{"name": "gap_hours", "value": 3}]},
            ),
            tool_use("get_stats", {"name": "totals", "params": [{"name": "top_n", "value": 3}]}),
        ),
        message(tool_use("finish", {"summary": "done"})),
    ]
    result, client = run(bean_goose, tmp_path, script)

    assert result.status == "finished"
    [overview] = results_of(client.requests[1])
    available = json.loads(overview["content"])["available_stats"]
    assert available["top_emoji"] == {
        "summary": "Most-used emoji per sender.",
        "params": {"top_n": 10},
    }
    assert available["totals"]["params"] == {}
    assert available["distinctive_phrases"]["params"]["min_ratio"] == 5.0
    # One top-N name everywhere.
    assert not any("k" in s["params"] for s in available.values())
    assert all("top_n" in available[s]["params"] for s in ("top_words", "top_ngrams"))

    emoji, phrases, totals = results_of(client.requests[2])
    assert not emoji.get("is_error")
    body = json.loads(emoji["content"])
    assert body["params"]["top_n"] == 3 and len(body["value"]["both"]) <= 3
    assert phrases["is_error"]
    assert phrases["content"] == (
        "distinctive_phrases does not take gap_hours;"
        " it takes: top_n, max_n, min_count, min_ratio, min_baseline_tokens"
    )
    assert totals["is_error"] and totals["content"] == "totals does not take top_n; it takes none"


def test_empty_search_query_browses_a_time_window(bean_goose, tmp_path):
    # Live runs: to read a period, the agent searched for dummy regexes like "." or .{25,}.
    may = {"start": "2021-05-01", "end": "2021-06-01", "mode": "keyword"}
    moment = {"start": "2021-05-15T03:48:00Z", "end": "2021-05-15T03:50:00Z", "mode": "regex"}
    script = [
        message(
            tool_use("search_messages", {"query": "", **may, "limit": 100}),
            tool_use("search_messages", {"query": "", **may, "order": "newest", "limit": 3}),
            tool_use("search_messages", {"query": "", **may, "mode": "regex", "sender": "me"}),
            tool_use("search_messages", {"query": "", **moment}),
        ),
        message(tool_use("finish", {"summary": "done"})),
    ]
    result, client = run(bean_goose, tmp_path, script)

    assert result.status == "finished"
    results = results_of(client.requests[1])
    assert not any(r.get("is_error") for r in results)
    oldest, newest, mine, around = (json.loads(r["content"]) for r in results)
    # Every 1:1 message in May 2021, capped, in time order.
    assert oldest["total_matches"] == 291 and oldest["returned"] == MAX_SEARCH
    times = [m["sent_at_utc"] for m in oldest["messages"]]
    assert times == sorted(times) and times[0] >= "2021-05-01"
    assert newest["total_matches"] == 291 and newest["returned"] == 3
    last = [m["sent_at_utc"] for m in newest["messages"]]
    assert last == sorted(last, reverse=True) and last[0] < "2021-06-01"
    assert 0 < mine["total_matches"] < 291
    assert all(m["sender"] == "me" for m in mine["messages"])
    # The end bound is exclusive; the reaction (000508) is out of scope.
    assert [m["id"] for m in around["messages"]] == [SID + "000507", SID + "000509"]

    # Every message shown went through the egress log, with its text.
    egress = json.loads((tmp_path / "run" / "egress.json").read_text())
    shown = {m["id"] for r in (oldest, newest, mine, around) for m in r["messages"]}
    logged = {e["message_id"] for e in egress["entries"] if e["with_text"]}
    assert shown == logged and egress["messages_shared"] == len(logged)
