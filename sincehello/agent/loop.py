"""LOOP 1: the discovery agent (SPEC.md §7.5), a hand-written tool-use loop.

The whole agent is one `while` loop around `messages`:

    send messages -> get a response -> append it -> run its tool calls
    -> append all results in ONE user message -> repeat

until the model calls `finish`, we hit the turn cap, or the dollar budget runs out.
Everything else in this file is a guard around that loop:

- Prompt caching: the system prompt and tools never change during a run (or between
  runs), so they are a cached prefix. Top-level `cache_control` also caches the growing
  history, so each turn pays full price only for what it appended.
- The history is append-only. Opus 5.5 ties each thinking block to the exact prefix it
  was produced after, so editing or deleting an earlier message would invalidate them
  (and the cache). Instructions we add mid-run (wrap up, refusal notes) are appended as
  `role: "system"` messages instead of edits to the system prompt.
- Refusals: server-side fallbacks (`fallbacks: "default"`) retry a declined request on
  another model. If the whole chain declines, we drop the partial output, note it, and
  carry on with other categories.
- Budget: a hard dollar cap computed from `usage` (budget.py), checked before every
  request, plus an advisory `task_budget` so the model paces itself. Critic calls made
  inside `propose_insight` spend from the same cap (verify/critic.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from sincehello.agent import prompts
from sincehello.agent.budget import Budget, task_budget_tokens
from sincehello.agent.context import ToolContext
from sincehello.agent.runlog import RunRecorder, as_dict
from sincehello.agent.tools import Critic, build_tools, dispatch
from sincehello.curate import CurationConfig
from sincehello.report.schema import Report
from sincehello.store import Store

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # pairs with fallbacks="default"
TASK_BUDGET_BETA = "task-budgets-2026-03-13"
MAX_CONSECUTIVE_REFUSALS = 3
MAX_NUDGES = 2  # times we ask the model to keep going after it stops without finish
WRAP_UP_TURNS_LEFT = 3


@dataclass
class AgentConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"  # low | medium | high | xhigh | max; the lever to tune in evals
    max_cost: float = 5.0  # USD, hard cap
    max_turns: int = 60
    max_tokens: int = 32_000  # per response, thinking included
    fallbacks: bool = True
    task_budget: bool = True
    default_tz: str | None = None
    deck: CurationConfig = field(default_factory=CurationConfig)  # SPEC.md §7.7


@dataclass
class RunResult:
    status: str  # finished | turn_cap | budget | refused | no_finish | api_error
    turns: int
    cost_usd: float
    report: Report
    run_dir: Path
    notes: list[str] = field(default_factory=list)


def build_request(config: AgentConfig, tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything but `messages`. Identical on every turn, which keeps the cache warm."""
    betas: list[str] = []
    output_config: dict[str, Any] = {"effort": config.effort}
    request: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "system": [
            {"type": "text", "text": prompts.load("system"), "cache_control": {"type": "ephemeral"}}
        ],
        "tools": tools,
        "tool_choice": {"type": "auto"},  # forced tool use isn't supported on Opus 5.5
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": output_config,
        "cache_control": {"type": "ephemeral"},  # also cache the growing history
    }
    if config.fallbacks:
        request["fallbacks"] = "default"
        betas.append(FALLBACK_BETA)
    if config.task_budget:
        total = task_budget_tokens(config.max_cost, config.model)
        output_config["task_budget"] = {"type": "tokens", "total": total}
        betas.append(TASK_BUDGET_BETA)
    request["betas"] = betas
    return request


def send(client: Any, request: dict[str, Any], messages: list[dict[str, Any]]) -> Any:
    """One API call. Streaming avoids HTTP timeouts on long thinking turns."""
    with client.beta.messages.stream(**request, messages=messages) as stream:
        return stream.get_final_message()


def replayable(content: list[Any]) -> list[dict[str, Any]]:
    """The response content to append to history, as plain dicts.

    After a mid-response fallback, the declined model's thinking and tool_use blocks
    (everything before the last `fallback` marker) must not be echoed back.
    """
    blocks = [as_dict(b) for b in content]
    last_switch = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=-1)
    return [
        b
        for i, b in enumerate(blocks)
        if b["type"] != "fallback"
        and not (i < last_switch and b["type"] in ("thinking", "redacted_thinking", "tool_use"))
    ]


def run_agent(
    store: Store,
    person_id: int,
    client: Any,
    config: AgentConfig,
    run_dir: Path,
    critic: Critic | None = None,
) -> RunResult:
    ctx = ToolContext(store, person_id, config.default_tz)
    tools = build_tools()
    tools_by_name = {t.name: t for t in tools}
    request = build_request(config, [t.definition() for t in tools])
    budget = Budget(config.max_cost)
    rec = RunRecorder(run_dir)
    # The critic (SPEC.md §7.6) runs inside propose_insight, in its own context, but
    # spends from this run's budget and writes `critic_*` events to this run's log.
    ctx.critic, ctx.budget, ctx.log = critic, budget, rec.event
    rec.event("request_static", request=request, person_id=person_id)

    kickoff = prompts.load("kickoff").format(name=ctx.person_name or "them", person_id=person_id)
    messages: list[dict[str, Any]] = [{"role": "user", "content": kickoff}]
    logged = 0  # how many messages the run log already has
    status, notes = "turn_cap", []
    wrapped_up, nudges, refusals_in_a_row, turn = False, 0, 0, 0

    try:
        while turn < config.max_turns:
            # --- 1. Guard: stop before a request we can't afford. ---------------
            if not budget.can_afford_next():
                status = "budget"
                break
            turn += 1
            rec.event("request", turn=turn, new_messages=messages[logged:])
            logged = len(messages)

            # --- 2. Call Claude. The SDK already retried 429s and 5xx. ----------
            try:
                response = send(client, request, messages)
            except anthropic.APIError as e:
                status = "api_error"
                notes.append(f"turn {turn}: {type(e).__name__}: {e}")
                rec.event("api_error", turn=turn, error=str(e), type=type(e).__name__)
                break
            cost = budget.add(response)
            rec.event("response", turn=turn, response=response, cost_usd=cost, total=budget.spent)

            # --- 3. Refusal: discard the partial output, note it, move on. ------
            if response.stop_reason == "refusal":
                refusals_in_a_row += 1
                category = getattr(response.stop_details, "category", None)
                notes.append(f"turn {turn}: refusal ({category})")
                if refusals_in_a_row >= MAX_CONSECUTIVE_REFUSALS:
                    status = "refused"
                    break
                # A system message must follow a user message; after our own note
                # (already last), a repeat refusal simply retries.
                if messages[-1]["role"] == "user":
                    cat = f" (category: {category})" if category else ""
                    note = prompts.load("refusal").format(category=cat)
                    messages.append({"role": "system", "content": note})
                continue
            refusals_in_a_row = 0

            # --- 4. Append the assistant turn exactly as returned. --------------
            content = replayable(response.content)
            messages.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if b["type"] == "tool_use"]

            if not tool_uses:
                # tool_choice is auto, so the model may stop talking without finish.
                if response.stop_reason == "max_tokens":
                    text = "Your last response hit the output limit. Continue, more briefly."
                elif nudges < MAX_NUDGES:
                    nudges += 1
                    text = prompts.load("nudge")
                else:
                    status = "no_finish"
                    break
                messages.append({"role": "user", "content": text})
                continue

            # --- 5. Run every tool call; all results go back in ONE message. ----
            # Calls run one after another (SQLite connections aren't shared across
            # threads, and these tools take milliseconds), but they are answered
            # together, which is what keeps the model making parallel calls.
            ctx.turn = turn
            results = []
            for call in tool_uses:
                if response.stop_reason == "max_tokens":
                    # The last block may be cut off mid-input; run nothing this turn.
                    content_text = (
                        "Not run: your response hit max_tokens before this call was"
                        " complete. Re-issue it, with fewer calls per turn."
                    )
                    is_error, internal = True, None
                else:
                    out = dispatch(tools_by_name, ctx, call["name"], call["input"])
                    content_text, is_error, internal = out.content, out.is_error, out.internal_error
                result = {"type": "tool_result", "tool_use_id": call["id"], "content": content_text}
                if is_error:
                    result["is_error"] = True
                results.append(result)
                rec.event(
                    "tool",
                    turn=turn,
                    name=call["name"],
                    input=call["input"],
                    is_error=is_error,
                    result=content_text,
                    internal_error=internal,
                )
            messages.append({"role": "user", "content": results})

            # --- 6. Stop, or steer. ----------------------------------------------
            if ctx.finished:
                status = "finished"
                break
            if not wrapped_up:
                reason = None
                if budget.should_wrap_up:
                    reason = "cost budget"
                elif config.max_turns - turn <= WRAP_UP_TURNS_LEFT:
                    reason = "turn limit"
                if reason:
                    wrapped_up = True
                    text = prompts.load("wrap_up").format(reason=reason)
                    messages.append({"role": "system", "content": text})
                    rec.event("wrap_up", turn=turn, reason=reason, spent=budget.spent)
    except BaseException as e:  # a bug or Ctrl-C: record it, still write the report
        status = "error"
        notes.append(f"{type(e).__name__}: {e}")
        raise
    finally:
        # Runs on every exit, errors included: keep what was found so far.
        rec.event("request", turn=None, new_messages=messages[logged:])
        rec.event(
            "end",
            status=status,
            turns=turn,
            cost_usd=budget.spent,
            critic_calls=budget.critic_calls,
            critic_cost_usd=budget.critic_spent,
            notes=notes,
        )
        report = rec.write_outputs(
            ctx,
            status=status,
            model=config.model,
            effort=config.effort,
            cost=budget.spent,
            tokens=budget.tokens,
            turns=turn,
            critic_calls=budget.critic_calls,
            critic_cost=budget.critic_spent,
            curation=config.deck,
        )
        rec.close()
    return RunResult(status, turn, budget.spent, report, run_dir, notes)
