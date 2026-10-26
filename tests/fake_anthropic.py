"""A scripted stand-in for `anthropic.Anthropic`, so agent tests never touch the network.

Responses are real SDK objects (`BetaMessage`) built from dicts shaped like API output,
so the loop is tested against the same types it sees in production.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from anthropic.types.beta import BetaMessage

_ids = itertools.count(1)

# A typical mid-run turn: mostly cache reads.
USAGE = {
    "input_tokens": 300,
    "output_tokens": 900,
    "cache_read_input_tokens": 12_000,
    "cache_creation_input_tokens": 1_500,
}


def tool_use(name: str, input: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids):04d}", "name": name, "input": input or {}}


def thinking(text: str = "") -> dict[str, Any]:
    # display="summarized" returns a summary; the signature is opaque and must round-trip.
    return {"type": "thinking", "thinking": text, "signature": f"sig{next(_ids)}"}


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def message(
    *content: dict[str, Any],
    stop_reason: str = "tool_use",
    usage: dict[str, Any] | None = None,
    model: str = "claude-opus-5-5",
    stop_details: dict[str, Any] | None = None,
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": f"msg_{next(_ids):04d}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": list(content),
            "stop_reason": stop_reason,
            "stop_details": stop_details,
            "stop_sequence": None,
            "usage": usage or USAGE,
        }
    )


def refusal(category: str = "bio") -> BetaMessage:
    return message(
        stop_reason="refusal",
        usage={"input_tokens": 300, "output_tokens": 0, "cache_read_input_tokens": 12_000},
        stop_details={"type": "refusal", "category": category, "explanation": "declined"},
    )


# A critic call (verify/critic.py): a cached system prompt, one fresh user message.
CRITIC_USAGE = {
    "input_tokens": 2_500,
    "output_tokens": 1_200,
    "cache_read_input_tokens": 1_800,
}


def critic_reply(
    verdict: str = "pass", issues: list[str] | None = None, **scores: int
) -> BetaMessage:
    """A critic response: structured output is the JSON text block."""
    out = {
        "issues": issues or [],
        "scores": {"accuracy": 5, "specificity": 4, "delight": 4, "tone": 5, **scores},
        "verdict": verdict,
    }
    return message(
        thinking("checked the excerpts"),
        text(json.dumps(out)),
        stop_reason="end_turn",
        usage=CRITIC_USAGE,
    )


def is_critic(request: dict[str, Any]) -> bool:
    return "format" in request.get("output_config", {})


Step = BetaMessage | Exception | Callable[[dict[str, Any]], BetaMessage]


class _Stream:
    def __init__(self, response: BetaMessage) -> None:
        self.response = response

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> BetaMessage:
        return self.response


class ScriptedClient:
    """`client.beta.messages.stream(**kwargs)` returns the next scripted response.

    A step can be a response, an exception to raise, or a function of the request.
    Every request is deep-copied into `requests` for assertions.
    """

    def __init__(self, script: list[Step]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs: Any) -> _Stream:
        self.requests.append(copy.deepcopy(kwargs))
        if not self.script:
            raise AssertionError("the agent made more requests than the script has")
        step = self.script.pop(0)
        if callable(step) and not isinstance(step, BetaMessage):
            step = step(kwargs)
        if isinstance(step, Exception):
            raise step
        return _Stream(step)
