"""The critic calibration set (evals/critic_calibration.py), offline."""

from __future__ import annotations

import json

import pytest
from fake_anthropic import ScriptedClient, critic_reply, refusal
from typer.testing import CliRunner

from evals import critic_calibration as cal
from sincehello import cli
from sincehello.agent.context import ToolContext
from sincehello.verify.critic import LLMCritic


def labelled(case):
    return critic_reply(case.label, [] if case.label == "pass" else [case.why])


@pytest.mark.parametrize("case", cal.CASES, ids=lambda c: c.id)
def test_every_case_reaches_the_critic(bean_goose, case):
    """Each case passes the deterministic checks, so only the critic can judge it."""
    store, pid = bean_goose
    ins = cal.prepare(ToolContext(store, pid, "America/New_York"), case)
    assert ins.verification.deterministic == "pass"
    assert case.label in ("pass", "revise", "reject")


def test_labels_cover_the_traps():
    labels = {c.id: c.label for c in cal.CASES}
    assert len(labels) >= 10
    assert {"pass", "revise", "reject"} == set(labels.values())
    for trap in ("wrong_sender", "wrong_date", "sarcasm", "group_chat_trap", "bland_plant"):
        assert trap in labels


def test_run_scores_agreement_and_respects_the_cap():
    script = [labelled(c) for c in cal.CASES]
    script[0] = critic_reply("revise", ["disagree"])  # one disagreement
    script[1] = refusal()  # one case with no verdict
    client = ScriptedClient(script)
    out = cal.run(LLMCritic(client), max_cost=0.50)
    n = len(cal.CASES)
    assert out["judged"] == n - 1 and out["calls"] == n
    assert out["agreement"] == round((n - 2) / (n - 1), 3)
    assert out["cases"][1]["error"] and out["cases"][0]["verdict"] == "revise"

    tiny = cal.run(LLMCritic(ScriptedClient([critic_reply()] * 3)), max_cost=0.03)
    assert tiny["calls"] < n and tiny["cost_usd"] <= 0.03 + 0.03  # one call's overshoot


def test_eval_critic_command(monkeypatch, tmp_path):
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path))
    client = ScriptedClient([labelled(c) for c in cal.CASES])
    monkeypatch.setattr(cli, "_anthropic_client", lambda: client)
    result = CliRunner().invoke(cli.app, ["eval", "critic", "--max-cost", "0.5"])
    assert result.exit_code == 0, result.output
    assert f"agreement {len(cal.CASES)}/{len(cal.CASES)}" in result.output
    (out,) = (tmp_path / "evals").glob("critic-calibration-*.json")
    assert json.loads(out.read_text())["agreement"] == 1.0
