from __future__ import annotations

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
