from __future__ import annotations

import pytest
from fake_anthropic import message, text

from sincehello.agent.budget import Budget, response_cost, task_budget_tokens


def test_cost_prices_cache_reads_and_writes():
    m = message(
        text("x"),
        usage={
            "input_tokens": 1_000,
            "output_tokens": 2_000,
            "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 10_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 6_000,
                "ephemeral_1h_input_tokens": 4_000,
            },
        },
    )
    cost, tokens = response_cost(m)
    # $4 in, $20 out, $0.20 cache read, $5 5-minute write, $8 1-hour write (per MTok).
    expected = (1_000 * 4 + 2_000 * 20 + 100_000 * 0.2 + 6_000 * 5 + 4_000 * 8) / 1e6
    assert cost == pytest.approx(expected)
    assert tokens == {
        "input": 1_000,
        "output": 2_000,
        "cache_write_5m": 6_000,
        "cache_write_1h": 4_000,
        "cache_read": 100_000,
    }


def test_writes_without_ttl_breakdown_count_as_5_minute():
    m = message(
        text("x"),
        usage={"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000},
    )
    assert response_cost(m)[0] == pytest.approx(5.0)


def test_unknown_model_is_priced_conservatively():
    m = message(
        text("x"), model="claude-new-thing", usage={"input_tokens": 0, "output_tokens": 1_000_000}
    )
    assert response_cost(m)[0] == pytest.approx(25.0)


def test_budget_projection_and_wrap_up():
    b = Budget(max_cost=1.0)
    assert b.can_afford_next() and not b.should_wrap_up
    turn = message(text("x"), usage={"input_tokens": 0, "output_tokens": 15_000})  # $0.30
    b.add(turn)
    b.add(turn)
    assert b.spent == pytest.approx(0.6) and b.can_afford_next()
    b.add(turn)
    assert b.should_wrap_up  # 90% >= 80%
    assert not b.can_afford_next()  # 0.9 + 0.3 > 1.0
    assert b.tokens["output"] == 45_000


def test_task_budget_tracks_the_dollar_cap():
    assert task_budget_tokens(5.0, "claude-opus-5-5") == 322_580
    assert task_budget_tokens(1.0, "claude-opus-5-5") == 64_516
    assert task_budget_tokens(0.1, "claude-opus-5-5") == 20_000  # API minimum
