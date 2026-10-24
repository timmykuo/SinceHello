from __future__ import annotations

import json

import pytest
from conftest import FakeConnector
from typer.testing import CliRunner

from sincehello.cli import app
from sincehello.connectors import registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_HOME", str(tmp_path))
    monkeypatch.setitem(registry.CONNECTORS, "fake", FakeConnector)


def test_connect_lists_status():
    result = runner.invoke(app, ["connect"])
    assert result.exit_code == 0
    assert "fake" in result.output and "ready" in result.output


def test_connect_then_people():
    result = runner.invoke(app, ["connect", "fake"])
    assert result.exit_code == 0, result.output
    assert "5 new of 5 messages" in result.output
    result = runner.invoke(app, ["people"])
    assert "Alex" in result.output


def test_unknown_connector():
    result = runner.invoke(app, ["connect", "nope"])
    assert result.exit_code == 1


def test_stats_text_and_json():
    runner.invoke(app, ["connect", "fake"])
    result = runner.invoke(app, ["stats", "2", "--tz", "UTC"])
    assert result.exit_code == 0, result.output
    assert "Stats for Alex" in result.output and "q_totals_" in result.output
    result = runner.invoke(app, ["stats", "2", "--json", "--tz", "UTC"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stats"]["totals"]["value"]["total"] == 3
    assert data["stats"]["who_texts_first"]["params"]["gap_hours"] == 6.0


def test_stats_unknown_person():
    result = runner.invoke(app, ["stats", "99"])
    assert result.exit_code == 1


def test_eras_and_candidates_on_the_sample(monkeypatch):
    monkeypatch.delenv("SINCEHELLO_SYNTHETIC_DIR", raising=False)
    result = runner.invoke(app, ["connect", "synthetic"])
    assert result.exit_code == 0, result.output
    alex = "2"
    result = runner.invoke(app, ["eras", alex])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("era_1 ") and "tz gap 14h" in result.output
    result = runner.invoke(app, ["eras", alex, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["eras"][0]["id"] == "era_1" and "signals" in data["eras"][0]

    result = runner.invoke(app, ["candidates", alex])
    assert result.exit_code == 0, result.output
    assert 'first "i love you"' in result.output and 'pet name "bean"' in result.output
    result = runner.invoke(app, ["candidates", alex, "--kind", "pet_name", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [c["kind"] for c in data["candidates"]] == ["pet_name"]
    assert data["candidates"][0]["details"]["term"] == "bean"


def test_eras_and_candidates_errors():
    runner.invoke(app, ["connect", "fake"])
    assert runner.invoke(app, ["eras", "99"]).exit_code == 1
    assert runner.invoke(app, ["candidates", "99"]).exit_code == 1
    assert runner.invoke(app, ["candidates", "2", "--kind", "nope"]).exit_code == 1
    result = runner.invoke(app, ["eras", "2", "--tz", "UTC"])
    assert result.exit_code == 0 and result.output.startswith("era_1 ")
