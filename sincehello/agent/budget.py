"""Hard dollar cap for one agent run (SPEC.md §7.5, §9).

Cost comes from each response's `usage`, priced per model with cache reads and writes
billed at their own rates. When a server-side fallback ran, `usage.iterations` lists
every attempt with its own model, and is the billing source of truth.

Agent turns and critic calls (SPEC.md §7.6) spend from the same cap but are tracked
apart: only agent turns feed the next-turn projection, and each critic call is guarded
by its own estimate (`can_afford_critic`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


# From the current price list. Cache writes are 1.25x (5 minute) / 2x (1 hour) input.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-opus-5": Price(5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-8": Price(5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 4.00, 0.20),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.125, 0.20, 0.01),
}
# Unknown models (e.g. a new fallback target) are priced like the dearest one we know,
# so the cap errs toward stopping early.
FALLBACK_PRICE = max(PRICES.values(), key=lambda p: p.output)

WRAP_UP_AT = 0.8  # share of the budget that triggers the wrap-up message
# What we assume the first critic call costs before any has been priced: ~1.5k cached
# system tokens, ~3k fresh input, ~2k output (thinking included) on Opus 5.5.
CRITIC_PRIOR = 0.06


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _tokens(usage: Any) -> dict[str, int]:
    write_5m = write_1h = 0
    cc = _get(usage, "cache_creation")
    if cc is not None:
        write_5m = _get(cc, "ephemeral_5m_input_tokens") or 0
        write_1h = _get(cc, "ephemeral_1h_input_tokens") or 0
    total_writes = _get(usage, "cache_creation_input_tokens") or 0
    if write_5m + write_1h < total_writes:  # no TTL breakdown: assume the 5 minute TTL
        write_5m = total_writes - write_1h
    return {
        "input": _get(usage, "input_tokens") or 0,
        "output": _get(usage, "output_tokens") or 0,
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
        "cache_read": _get(usage, "cache_read_input_tokens") or 0,
    }


def _price(tokens: dict[str, int], model: str) -> float:
    p = PRICES.get(model, FALLBACK_PRICE)
    return (
        tokens["input"] * p.input
        + tokens["output"] * p.output
        + tokens["cache_write_5m"] * p.cache_write_5m
        + tokens["cache_write_1h"] * p.cache_write_1h
        + tokens["cache_read"] * p.cache_read
    ) / 1_000_000


def response_cost(response: Any) -> tuple[float, dict[str, int]]:
    """(USD, token counts) for one response, summing fallback attempts if any."""
    usage = response.usage
    attempts = _get(usage, "iterations") or []
    if attempts:
        parts = [(_tokens(a), _get(a, "model") or response.model) for a in attempts]
    else:
        parts = [(_tokens(usage), response.model)]
    cost = sum(_price(t, m) for t, m in parts)
    totals = {k: sum(t[k] for t, _ in parts) for k in parts[0][0]}
    return cost, totals


@dataclass
class Budget:
    max_cost: float
    spent: float = 0.0
    turns: int = 0  # agent turns only
    last_turn: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)  # agent and critic together
    critic_calls: int = 0
    critic_spent: float = 0.0
    critic_last: float = 0.0

    def add(self, response: Any, kind: str = "agent") -> float:
        """Charge one response to the run. `kind` is "agent" or "critic"."""
        cost, tokens = response_cost(response)
        self.spent += cost
        if kind == "critic":
            self.critic_calls += 1
            self.critic_spent += cost
            self.critic_last = cost
        else:
            self.turns += 1
            self.last_turn = cost
        for k, v in tokens.items():
            self.tokens[k] = self.tokens.get(k, 0) + v
        return cost

    def projected_next(self) -> float:
        """The next turn costs about as much as the larger of the last turn and the
        average turn: the context only grows, so turns get dearer over a run."""
        if not self.turns:
            return 0.0
        return max(self.last_turn, self.spent / self.turns)

    def can_afford_next(self) -> bool:
        return self.spent + self.projected_next() <= self.max_cost

    def projected_critic(self) -> float:
        if not self.critic_calls:
            return CRITIC_PRIOR
        return max(self.critic_last, self.critic_spent / self.critic_calls)

    def can_afford_critic(self) -> bool:
        return self.spent + self.projected_critic() <= self.max_cost

    @property
    def should_wrap_up(self) -> bool:
        return self.spent >= WRAP_UP_AT * self.max_cost


def task_budget_tokens(max_cost: float, model: str) -> int:
    """An advisory `task_budget` (tokens) roughly matching the dollar cap.

    The task budget counts what the model generates plus the tool results it reads, so
    price it at the output rate and keep 40% headroom for the re-read history, which it
    doesn't count but we pay for. The API minimum is 20,000.
    """
    p = PRICES.get(model, FALLBACK_PRICE)
    return max(20_000, int(0.6 * max_cost / p.output * 1_000_000))
