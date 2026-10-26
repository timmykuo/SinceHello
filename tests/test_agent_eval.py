"""`sincehello analyze` and `sincehello eval agent`, with the scripted client."""

from __future__ import annotations

import json

import pytest
from fake_anthropic import ScriptedClient, critic_reply, is_critic, message, tool_use
from test_agent_loop import ILY_INSIGHT, SID
from typer.testing import CliRunner

from evals import agent_recall, generator
from sincehello import cli
from sincehello.connectors.synthetic import SAMPLE_DIR

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path))


def fake_client(monkeypatch, script):
    client = ScriptedClient(script)
    monkeypatch.setattr(cli, "_anthropic_client", lambda: client)
    return client


def test_recall_scoring():
    records, manifest = generator.read(SAMPLE_DIR)
    insights = [
        {"id": "ins_001", "evidence": [{"message_id": SID + "000509"}], "metric": None},
        {"id": "ins_002", "evidence": [{"message_id": SID + "000228"}], "metric": None},
        {"id": "ins_003", "evidence": [{"message_id": SID + "001821"}]},  # on moving day
        {"id": "ins_004", "evidence": [], "metric": {"query_id": "q_reply_times_abc"}},
        {"id": "ins_005", "evidence": [{"message_id": SID + "000215"}]},  # group chat trap
    ]
    scored = agent_recall.score(manifest, records, insights)
    by_id = {f["id"]: f for f in scored["facts"]}
    assert by_id["first_ily"]["found"] and not by_id["first_ily"]["exact"]  # the reply only
    assert by_id["goose_incident"]["found"]  # origin exchange
    assert by_id["moving_day"]["insights"] == ["ins_003"]
    assert by_id["reply_speed"]["found"] and not by_id["texts_first"]["found"]
    assert scored["found"] == 4 and scored["total"] == 9 and scored["recall"] == 0.4444


def test_analyze_command(monkeypatch, tmp_path):
    assert runner.invoke(cli.app, ["connect", "synthetic"]).exit_code == 0
    client = fake_client(
        monkeypatch,
        [
            message(tool_use("get_overview")),
            message(tool_use("propose_insight", ILY_INSIGHT)),
            critic_reply("pass"),
            message(tool_use("finish", {"summary": "ok"})),
        ],
    )
    result = runner.invoke(
        cli.app, ["analyze", "2", "--max-cost", "1", "--effort", "medium", "--critic-effort", "low"]
    )
    assert result.exit_code == 0, result.output
    assert "finished: 1 insights in 3 turns" in result.output
    assert "critic: 1 calls" in result.output and "kept 1 pass" in result.output
    assert client.requests[0]["output_config"]["effort"] == "medium"
    assert [is_critic(r) for r in client.requests] == [False, False, True, False]
    assert client.requests[2]["output_config"]["effort"] == "low"
    (run_dir,) = (tmp_path / "runs").iterdir()
    assert {p.name for p in run_dir.iterdir()} == {"run_log.jsonl", "report.json", "egress.json"}


def test_analyze_unknown_person(monkeypatch):
    fake_client(monkeypatch, [])
    result = runner.invoke(cli.app, ["analyze", "99"])
    assert result.exit_code == 1 and "unknown person" in result.output


def test_eval_agent_command(monkeypatch, tmp_path):
    fake_client(
        monkeypatch,
        [
            message(tool_use("propose_insight", ILY_INSIGHT)),
            message(tool_use("finish", {"summary": "ok"})),
        ],
    )
    result = runner.invoke(
        cli.app, ["eval", "agent", "--sample", "bean-goose", "--max-cost", "1", "--no-critic"]
    )
    assert result.exit_code == 0, result.output
    assert "recall 1/9 (11%)" in result.output and "critic:" not in result.output
    assert "[x] first_ily" in result.output
    (run_dir,) = (tmp_path / "runs").iterdir()
    scored = json.loads((run_dir / "eval.json").read_text())
    assert scored["found"] == 1 and scored["exact"] == 1


def test_bad_effort(monkeypatch):
    fake_client(monkeypatch, [])
    assert runner.invoke(cli.app, ["analyze", "2", "--effort", "extreme"]).exit_code == 1


def test_eval_unknown_sample(monkeypatch):
    fake_client(monkeypatch, [])
    result = runner.invoke(cli.app, ["eval", "agent", "--sample", "nope"])
    assert result.exit_code == 1


def test_eval_agent_runs_the_critic_by_default(monkeypatch, tmp_path):
    client = fake_client(
        monkeypatch,
        [
            message(tool_use("propose_insight", ILY_INSIGHT)),
            critic_reply("revise", ["The date should be May 14 in their local time."]),
            message(tool_use("finish", {"summary": "ok"})),
        ],
    )
    result = runner.invoke(cli.app, ["eval", "agent", "--sample", "bean-goose", "--max-cost", "1"])
    assert result.exit_code == 0, result.output
    assert "recall 0/9" in result.output and "critic: 1 calls" in result.output
    assert sum(map(is_critic, client.requests)) == 1
