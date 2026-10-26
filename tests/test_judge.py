"""The eval judge, human ratings and judge calibration (evals/judge, SPEC.md §10.2-10.4).

All offline: the judge talks to a ScriptedClient, and every case is invented.
"""

from __future__ import annotations

import json
import random

import pytest
from fake_anthropic import ScriptedClient, message, refusal, text, thinking
from typer.testing import CliRunner

from evals import judge_calibration as jcal
from evals import ratings, runs
from evals.judge import DIMENSIONS, JudgeConfig, JudgeUnavailable, LLMJudge
from evals.judge.agreement import agreement
from evals.judge.judge import build_request, judge_insights, prompt, summarize
from sincehello import cli
from sincehello.agent.budget import Budget
from sincehello.agent.context import ToolContext
from sincehello.report.schema import Report, RunInfo
from sincehello.verify.critic import system_prompt as critic_prompt

USAGE = {"input_tokens": 2_500, "output_tokens": 1_000, "cache_read_input_tokens": 1_800}


def judge_reply(keep: bool = True, notes: list[str] | None = None, **scores: int):
    out = {
        "notes": notes or [],
        "scores": {d: 4 for d in DIMENSIONS} | scores,
        "keep": keep,
    }
    return message(thinking("read it"), text(json.dumps(out)), stop_reason="end_turn", usage=USAGE)


def pair_reply(winner: str, reasons: str = "deck_1 has the goose."):
    out = {"reasons": reasons, "winner": winner}
    return message(text(json.dumps(out)), stop_reason="end_turn", usage=USAGE)


def labelled(case_id: str):
    """A reply that matches a calibration label exactly (unlabelled dimensions at 3)."""
    label = jcal.LABELS[case_id]
    return judge_reply(bool(label.keep), **{d: 3 for d in DIMENSIONS} | label.scores)


@pytest.fixture
def cal_ctx(bean_goose):
    store, pid = bean_goose
    ctx = ToolContext(store, pid, "America/New_York")
    return ctx, jcal.prepare(ctx)


def test_requests_are_stable_cacheable_and_not_the_critic():
    for mode, fields in (
        ("judge", ["notes", "scores", "keep"]),
        ("pairwise", ["reasons", "winner"]),
    ):
        req = build_request(JudgeConfig(), mode)
        assert req == build_request(JudgeConfig(), mode)
        assert req["system"] == [
            {"type": "text", "text": prompt(mode), "cache_control": {"type": "ephemeral"}}
        ]
        assert list(req["output_config"]["format"]["schema"]["properties"]) == fields
        assert "tools" not in req and req["thinking"]["type"] == "adaptive"
        assert "{" not in prompt(mode)  # no run-specific slots in the cached prefix
    scores = build_request(JudgeConfig(), "judge")["output_config"]["format"]["schema"]
    assert list(scores["properties"]["scores"]["properties"]) == list(DIMENSIONS)
    # A separate prompt and rubric from the pipeline critic (SPEC.md §10.2).
    assert prompt("judge") != critic_prompt() and "non_obviousness" in prompt("judge")
    assert "non_obviousness" not in critic_prompt()


def test_score_shows_the_card_but_not_the_critic(cal_ctx):
    ctx, insights = cal_ctx
    goose = next(i for i in insights if i.id == "cal_goose_correct")
    goose.verification.critic = "pass"
    goose.scores.delight = 5
    client = ScriptedClient([judge_reply(notes=["Their own joke."], delight=5)])
    judge = LLMJudge(client, Budget(1.0))
    j = judge.score(goose, ctx)
    assert j.scores["delight"] == 5 and j.keep and j.notes == ["Their own joke."]

    (msg,) = client.requests[0]["messages"]
    body = msg["content"]
    card = json.loads(body.split("<card>")[1].split("</card>")[0])
    assert set(card) == {"category", "kind", "title", "headline", "body", "evidence"}
    assert "verification" not in body and "cal_goose_correct" not in body
    metric = json.loads(body.split("<metric>")[1].split("</metric>")[0])
    assert metric["query"]["kind"] == "count" and metric["value"] == goose.metric.value
    convo = body.split("<conversation>\n")[1].split("\n</conversation>")[0].splitlines()
    cited = [json.loads(x)["id"] for x in convo if x != "…" and json.loads(x).get("cited")]
    assert cited == [ev.message_id for ev in goose.evidence]
    assert {e["tool"] for e in ctx.egress.entries} == {"judge"}


def test_unknown_query_and_missing_messages(cal_ctx):
    ctx, insights = cal_ctx
    goose = next(i for i in insights if i.id == "cal_goose_correct")
    gone = goose.model_copy(
        update={
            "evidence": [
                *goose.evidence,
                goose.evidence[0].model_copy(update={"message_id": "x:1"}),
            ]
        }
    )
    client = ScriptedClient([judge_reply()])
    LLMJudge(client, Budget(1.0)).score(gone, ctx, queries={})
    body = client.requests[0]["messages"][0]["content"]
    assert '"query":"unknown query"' in body and '"missing_from_store":["x:1"]' in body


def test_retry_refusal_and_budget(cal_ctx):
    ctx, insights = cal_ctx
    bad = message(text("{not json"), stop_reason="end_turn", usage=USAGE)
    events = []
    client = ScriptedClient([bad, judge_reply(), refusal(), bad, bad])
    judge = LLMJudge(client, Budget(1.0), log=lambda k, **d: events.append(k))
    assert judge.score(insights[0], ctx).keep  # malformed once, then fine
    with pytest.raises(JudgeUnavailable, match="declined"):
        judge.score(insights[0], ctx)
    with pytest.raises(JudgeUnavailable, match="malformed"):
        judge.score(insights[0], ctx)
    assert events.count("judge_request_static") == 1 and "judge_malformed" in events
    assert judge.budget.critic_calls == 5

    tiny = LLMJudge(ScriptedClient([judge_reply()] * 20), Budget(0.08))
    out = judge_insights(tiny, insights, ctx)
    assert 0 < out["calls"] < len(insights)
    assert out["cost_usd"] <= 0.08
    assert any("budget" in (r["error"] or "") for r in out["insights"])


def test_compare_randomizes_the_order(cal_ctx):
    _, insights = cal_ctx
    a, b = insights[:3], insights[3:5]
    seen = set()
    for seed in range(8):
        rng = random.Random(seed)
        a_first = random.Random(seed).random() < 0.5
        client = ScriptedClient([pair_reply("deck_1")])
        res = LLMJudge(client, Budget(1.0)).compare({"me": None, "them": "Alex"}, a, b, rng)
        assert res.shown_first == ("a" if a_first else "b")
        assert res.winner == res.shown_first  # deck_1 maps back to whoever was shown first
        body = client.requests[0]["messages"][0]["content"]
        first = json.loads(body.split("<deck_1>")[1].split("</deck_1>")[0])
        assert first[0]["title"] == (a if a_first else b)[0].title
        seen.add(res.shown_first)
    assert seen == {"a", "b"}
    tie = LLMJudge(ScriptedClient([pair_reply("tie")]), Budget(1.0)).compare({}, a, b)
    assert tie.winner == "tie"


def test_agreement_and_summary():
    judge = {
        "x": {"scores": {d: 4 for d in DIMENSIONS}, "keep": True},
        "y": {"scores": {d: 2 for d in DIMENSIONS}, "keep": False},
        "z": {"scores": None, "keep": None},  # the judge failed on it
    }
    human = {
        "x": {"scores": {"accuracy": 5, "delight": 2}, "keep": True},
        "y": {"scores": {"accuracy": 2}, "keep": True},
        "z": {"scores": {"accuracy": 1}, "keep": False},
        "w": {"scores": {"accuracy": 5}, "keep": True},  # judge never saw it
    }
    out = agreement(judge, human)
    assert out["n"] == 2
    acc = out["dimensions"]["accuracy"]
    assert acc == {"n": 2, "exact": 0.5, "within_1": 1.0, "mae": 0.5, "bias": -0.5}
    assert out["dimensions"]["delight"]["within_1"] == 0.0
    assert "tone" not in out["dimensions"]
    assert out["keep"] == {"n": 2, "agreement": 0.5}

    s = summarize(
        [
            {"scores": judge["x"]["scores"], "keep": True},
            {"scores": judge["y"]["scores"], "keep": False},
            {"scores": None},
        ]
    )
    assert s["scored"] == 2 and s["means"]["overall"] == 3.0
    assert s["keep_rate"] == 0.5 and s["hallucination_rate"] == 0.5


def test_parse_scores():
    want = dict(zip(DIMENSIONS, [5, 4, 4, 5, 3], strict=True))
    for s in ("5 4 4 5 3", "54453", "5,4,4,5,3"):
        assert ratings.parse_scores(s) == want
    for bad in ("5 4 4", "64453", "5 4 4 5 33", ""):
        with pytest.raises(ValueError):
            ratings.parse_scores(bad)


def test_calibration_cases_and_labels(cal_ctx):
    _, insights = cal_ctx
    assert [i.id for i in insights] == sorted(jcal.reference(), key=[i.id for i in insights].index)
    assert set(jcal.LABELS) == {c.id for c in jcal.CASES}
    for label in jcal.LABELS.values():
        assert set(label.scores) <= set(DIMENSIONS)
        assert all(1 <= v <= 5 for v in label.scores.values())
        if label.keep:
            assert label.scores.get("accuracy", 5) >= 4  # the rubric's keep rule
    assert {"obvious_volume", "mean_sushi", "bland_plant", "sarcasm"} <= set(jcal.LABELS)


def test_calibration_run_reports_agreement():
    script = [labelled(c.id) for c in jcal.CASES]
    script[0] = judge_reply(False, accuracy=1)  # one disagreement on ily_correct
    out = jcal.run(LLMJudge(ScriptedClient(script), Budget(2.0)))
    a = out["agreement"]
    n = len(jcal.CASES)
    assert out["scored"] == n and a["n"] == n
    assert a["keep"]["n"] == n - 1  # number_without_metric has no keep label
    assert a["keep"]["agreement"] == round((n - 2) / (n - 1), 3)
    assert a["dimensions"]["accuracy"]["within_1"] == round((n - 1) / n, 3)
    assert a["dimensions"]["tone"]["within_1"] == 1.0


# --- the CLI, on a hand-built eval run of the bean-goose sample ----------------------


@pytest.fixture
def eval_run(tmp_path, monkeypatch, bean_goose):
    """A finished `eval agent` run dir holding the calibration insights as its report."""
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path / "home"))
    store, pid = bean_goose
    ctx = ToolContext(store, pid, "America/New_York")
    insights = jcal.prepare(ctx)[:4]
    for ins in insights:
        ins.verification.critic = "pass"

    def make(name: str, keep: int) -> str:
        run_dir = runs.runs_dir() / name
        run_dir.mkdir(parents=True)
        report = Report(
            person={"id": pid, "name": "Alex Chen"},
            range={"first_utc": None, "last_utc": None},
            sources=["synthetic"],
            deck=[],
            insights=insights[:keep],
            eras=[],
            stats={},
            summary=None,
            run=RunInfo(
                run_id=name,
                status="finished",
                cost_usd=1.0,
                tokens={},
                turns=10,
                messages_shared=40,
                pct_shared=1.5,
                model="claude-opus-5-5",
                effort="high",
                version="0.0.1",
            ),
        )
        (run_dir / "report.json").write_text(report.model_dump_json())
        # The goose metric's count, as the agent's tool call logged it.
        qid = next(i.metric.query_id for i in insights if i.metric)
        args = ctx.queries[qid]["args"]
        event = {
            "event": "tool",
            "name": "count",
            "input": args,
            "is_error": False,
            "result": json.dumps({"query_id": qid, "total": 1}),
        }
        (run_dir / "run_log.jsonl").write_text(json.dumps(event) + "\n")
        return name

    return make("20260101T000000Z-p2-aaaa-eval-bean-goose", 4), make(
        "20260102T000000Z-p2-bbbb-eval-bean-goose", 2
    )


def test_cli_judge_rate_agreement_compare(eval_run, monkeypatch):
    run_a, run_b = eval_run
    runner = CliRunner()
    client = ScriptedClient(
        [judge_reply(), judge_reply(False, accuracy=2), judge_reply(), judge_reply()]
    )
    monkeypatch.setattr(cli, "_anthropic_client", lambda: client)
    result = runner.invoke(cli.app, ["eval", "judge", run_a, "--max-cost", "1"])
    assert result.exit_code == 0, result.output
    assert "judged 4/4" in result.output and "hallucination 0.25" in result.output
    run_dir = runs.resolve(run_a)
    judged = json.loads((run_dir / "judge.json").read_text())
    assert judged["judge"]["prompt_version"] and judged["egress"]["messages_shown"] > 0
    goose = client.requests[1]["messages"][0]["content"]
    assert '"kind":"count"' in goose  # the query came back from the run log
    assert (run_dir / "judge_log.jsonl").is_file()

    # Rate: two cards, a bad entry, a skip, then quit; resume rates the rest.
    answers = "4 4 4 4 4\ny\nnice\n9\n2 2 2 2 2\nn\n\ns\nq\n"
    result = runner.invoke(cli.app, ["eval", "rate", run_a], input=answers)
    assert result.exit_code == 0, result.output
    assert "give 5 scores" in result.output
    saved = ratings.load(run_dir)
    assert list(saved) == ["cal_ily_correct", "cal_goose_correct"]
    assert saved["cal_ily_correct"].note == "nice" and not saved["cal_goose_correct"].keep
    result = runner.invoke(cli.app, ["eval", "rate", run_a], input="3 3 3 3 3\ny\n\nq\n")
    assert "2 to rate, 2 already rated" in result.output and len(ratings.load(run_dir)) == 3

    result = runner.invoke(cli.app, ["eval", "agreement", run_a])
    assert result.exit_code == 0, result.output
    assert "3 insights scored by both" in result.output
    assert "accuracy         n   3  exact 67%" in result.output

    client.script = [pair_reply("deck_2", "B is tighter.")]
    result = runner.invoke(cli.app, ["eval", "compare", run_a, run_b, "--pairwise", "--seed", "1"])
    assert result.exit_code == 0, result.output
    assert "judge keep" in result.output and "0.75" in result.output
    a_first = random.Random(1).random() < 0.5
    assert f"pairwise: {'B' if a_first else 'A'} (" in result.output
    (pair,) = runs.resolve(run_b).glob("pairwise-vs-*.json")
    assert json.loads(pair.read_text())["reasons"] == "B is tighter."


def test_cli_judge_errors(eval_run, monkeypatch):
    run_a, _ = eval_run
    runner = CliRunner()
    assert runner.invoke(cli.app, ["eval", "judge", "nope"]).exit_code == 1
    assert runner.invoke(cli.app, ["eval", "agreement", run_a]).exit_code == 1  # no judge.json
    monkeypatch.setattr(cli, "_anthropic_client", lambda: ScriptedClient([]))
    result = runner.invoke(cli.app, ["eval", "judge", run_a, "--sample", "nope"])
    assert result.exit_code == 1 and "no sample" in result.output


def test_cli_judge_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path))
    client = ScriptedClient([labelled(c.id) for c in jcal.CASES])
    monkeypatch.setattr(cli, "_anthropic_client", lambda: client)
    result = CliRunner().invoke(cli.app, ["eval", "judge-calibration", "--max-cost", "2"])
    assert result.exit_code == 0, result.output
    assert f"{len(jcal.CASES)} insights scored by both" in result.output
    (out,) = (tmp_path / "evals").glob("judge-calibration-*.json")
    assert json.loads(out.read_text())["agreement"]["keep"]["agreement"] == 1.0
