"""Deterministic insight checks (SPEC.md §7.6 step 1). Cheap, local, no LLM.

`check(ctx, draft)` returns a list of failure reasons; empty means it passed. The
reasons go straight back to the agent as an `is_error` tool result, so they are written
to tell the model what to fix.
"""

from __future__ import annotations

import json
import math
from typing import Any

from sincehello.agent.context import ToolContext, ToolError
from sincehello.report.schema import InsightDraft


def check(ctx: ToolContext, draft: InsightDraft, era_ids: set[str] | None = None) -> list[str]:
    failures: list[str] = []

    # Every cited message exists, is in this person's 1:1 conversations, and is not a
    # reaction; each quote appears verbatim in its message.
    for ev in draft.evidence:
        try:
            m = ctx.get(ev.message_id)
        except ToolError as e:
            failures.append(f"evidence {ev.message_id}: {e}")
            continue
        if _norm(ev.quote) not in _norm(m.text):
            failures.append(
                f"evidence {ev.message_id}: quote {ev.quote!r} is not in the message text"
                " (quotes must be copied verbatim)"
            )

    # "First" claims: re-run first_occurrence; the true first match must be cited.
    if draft.first is not None:
        try:
            first = ctx.first_match(draft.first.pattern, draft.first.sender)
        except ToolError as e:
            failures.append(f"first: {e}")
        else:
            cited = {ev.message_id for ev in draft.evidence}
            if first["message_id"] is None:
                failures.append(f"first: pattern {draft.first.pattern!r} matches no message")
            elif first["message_id"] not in cited:
                failures.append(
                    f"first: the earliest match for {draft.first.pattern!r}"
                    f" (sender={draft.first.sender}) is {first['message_id']}"
                    f" at {first['sent_at_utc']}, which the evidence doesn't cite"
                )

    # Numeric claims: the query must recompute to the claimed value.
    if draft.metric is not None:
        try:
            value = ctx.recompute(draft.metric.query_id)
            actual = at_path(value, draft.metric.path)
        except ToolError as e:
            failures.append(f"metric: {e}")
        else:
            claimed = _parse(draft.metric.value)
            if not _same(claimed, actual):
                failures.append(
                    f"metric: {draft.metric.query_id} at path {draft.metric.path!r} is"
                    f" {json.dumps(actual, ensure_ascii=False)}, not"
                    f" {json.dumps(claimed, ensure_ascii=False)}"
                )

    if draft.era_id is not None and era_ids is not None and draft.era_id not in era_ids:
        failures.append(f"era_id {draft.era_id!r} is not one of {sorted(era_ids)}")
    return failures


def _norm(s: str) -> str:
    """Whitespace runs and curly quotes don't count as differences; everything else does."""
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(s.split())


def at_path(value: Any, path: str) -> Any:
    cur = value
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif (
            isinstance(cur, list)
            and part.lstrip("-").isdigit()
            and -len(cur) <= int(part) < len(cur)
        ):
            cur = cur[int(part)]
        else:
            raise ToolError(f"path {path!r} not found in the query result (failed at {part!r})")
    return cur


def _parse(claimed: Any) -> Any:
    """Tool inputs carry the claimed value as a string: JSON, or a bare string."""
    if isinstance(claimed, str):
        try:
            return json.loads(claimed)
        except json.JSONDecodeError:
            pass
    return claimed


def _same(claimed: Any, actual: Any) -> bool:
    if isinstance(actual, bool) or isinstance(claimed, bool):
        return claimed == actual
    if isinstance(actual, int | float) and isinstance(claimed, int | float):
        return math.isclose(claimed, actual, rel_tol=1e-9, abs_tol=1e-9)
    return claimed == actual or str(claimed) == str(actual)
