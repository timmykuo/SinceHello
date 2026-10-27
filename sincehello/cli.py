"""Command-line interface (SPEC.md §11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from sincehello import config
from sincehello.analysis import compute_all
from sincehello.analysis.eras import detect_eras
from sincehello.analysis.miners import KINDS, mine
from sincehello.analysis.render import render
from sincehello.connectors import registry
from sincehello.store import Store, identity

app = typer.Typer(help="Wrapped-style insights from your messages with one person.")


@app.command()
def connect(
    connector_id: Annotated[str | None, typer.Argument(help="Connector to ingest.")] = None,
) -> None:
    """List connectors and their status, or ingest from one."""
    if connector_id is None:
        connectors = registry.all_connectors()
        if not connectors:
            typer.echo("No connectors available yet.")
        for c in connectors:
            status = c.check()
            typer.echo(f"{c.id:<12} {c.kind:<10} {status.state:<12} {status.detail}")
        return

    try:
        connector = registry.get(connector_id)
    except KeyError as e:
        typer.echo(str(e.args[0]), err=True)
        raise typer.Exit(1) from None
    status = connector.check()
    if status.state != "ready":
        typer.echo(f"{connector.id}: {status.state}. {status.detail}", err=True)
        for i, step in enumerate(connector.setup_instructions(), 1):
            typer.echo(f"  {i}. {step}", err=True)
        raise typer.Exit(1)
    with Store(config.store_path()) as store:
        report = connector.ingest(store)
    typer.echo(
        f"{report.source}: {report.messages_added} new of {report.messages_seen} messages,"
        f" {report.conversations} conversations"
    )
    for w in report.warnings:
        typer.echo(f"warning: {w}", err=True)


@app.command()
def people(limit: Annotated[int, typer.Option(help="Max rows.")] = 20) -> None:
    """List people by message count (1:1 conversations only)."""
    with Store(config.store_path()) as store:
        rows = store.people()[:limit]
    if not rows:
        typer.echo("No messages yet. Run `sincehello connect <connector>` first.")
    for p in rows:
        name = p.display_name or (p.handles[0] if p.handles else f"person {p.person_id}")
        typer.echo(f"{p.person_id:>5}  {p.message_count:>8}  {name}  ({', '.join(p.handles)})")


@app.command()
def stats(
    person_id: Annotated[int, typer.Argument(help="Person id, from `sincehello people`.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    gap_hours: Annotated[
        float, typer.Option(help="Silence that makes the next message a new conversation.")
    ] = 6.0,
    tz: Annotated[str | None, typer.Option(help="Timezone for people with no tz periods.")] = None,
) -> None:
    """Local stats for your 1:1 conversations with one person (no LLM)."""
    with Store(config.store_path()) as store:
        try:
            results = compute_all(
                store,
                person_id,
                default_tz=tz,
                gap_hours=gap_hours,
                max_reply_hours=gap_hours,
            )
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
        name = next(
            (p.display_name or p.handles[0] for p in store.people() if p.person_id == person_id),
            f"person {person_id}",
        )
    if as_json:
        out = {"person_id": person_id, "stats": {k: r.to_dict() for k, r in results.items()}}
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        return
    for line in render(results, name):
        typer.echo(line)


@app.command()
def eras(
    person_id: Annotated[int, typer.Argument(help="Person id, from `sincehello people`.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    min_weeks: Annotated[int, typer.Option(help="Shortest era, in weeks.")] = 6,
    max_eras: Annotated[int, typer.Option(help="Most eras to return.")] = 8,
    tz: Annotated[str | None, typer.Option(help="Timezone for people with no tz periods.")] = None,
) -> None:
    """Chapters of your history with one person, from weekly signals (no LLM)."""
    with Store(config.store_path()) as store:
        try:
            found = detect_eras(
                store, person_id, min_weeks=min_weeks, max_eras=max_eras, default_tz=tz
            )
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
    if as_json:
        out = {"person_id": person_id, "eras": [e.to_dict() for e in found]}
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not found:
        typer.echo("No messages with this person.")
    for e in found:
        s = e.signals
        reply = f"{s['reply_median_s']:.0f}s" if "reply_median_s" in s else "-"
        typer.echo(
            f"{e.id:<7} {e.start_utc:%Y-%m-%d} -> {e.end_utc:%Y-%m-%d}"
            f"  {e.message_count:>6} msgs  {s['messages_per_week']:>6.1f}/wk"
            f"  reply {reply:>6}  late {s['late_night_share']:.0%}"
            f"  affection {s['affection_share']:.0%}  tz gap {s['tz_gap_hours']:g}h"
        )


@app.command()
def candidates(
    person_id: Annotated[int, typer.Argument(help="Person id, from `sincehello people`.")],
    kind: Annotated[str | None, typer.Option(help=f"Only this kind: {', '.join(KINDS)}.")] = None,
    limit: Annotated[int, typer.Option(help="Max candidates.")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    tz: Annotated[str | None, typer.Option(help="Timezone for people with no tz periods.")] = None,
) -> None:
    """Leads for the agent: firsts, pet names, spikes, conflicts (no LLM)."""
    with Store(config.store_path()) as store:
        try:
            found = mine(store, person_id, kind, limit=limit, default_tz=tz)
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
    if as_json:
        out = {"person_id": person_id, "candidates": [c.to_dict() for c in found]}
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not found:
        typer.echo("No candidates.")
    for c in found:
        ids = ", ".join(map(str, c.message_ids[:5])) + (" ..." if len(c.message_ids) > 5 else "")
        typer.echo(f"{c.score:.2f}  {c.kind:<9} {c.label}  [messages {ids}]")


tz_app = typer.Typer(help="Timezone periods per person: inferred, then confirmed (SPEC.md §6.1).")
app.add_typer(tz_app, name="tz")

TzOption = Annotated[str | None, typer.Option(help="Timezone for people with no tz periods.")]


def _person_name(store: Store, person_id: int) -> str:
    for p in store.people():
        if p.person_id == person_id:
            return p.display_name or (p.handles[0] if p.handles else f"person {person_id}")
    typer.echo(f"unknown person {person_id} (see `sincehello people`)", err=True)
    raise typer.Exit(1)


def _local(ms: int, tz: str):
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(ZoneInfo(tz))


def _day(ms: int, tz: str) -> str:
    d = _local(ms, tz)
    return f"{d:%b} {d.day}, {d.year}"


def _describe(store: Store, sg, name: str, tz: str) -> str:
    """One suggestion as a question, e.g. "Looks like Alex was about 14h ahead of you..."."""
    from sincehello.analysis.tz import TzResolver
    from sincehello.analysis.tz_infer import midpoint_ms, offset_label
    from sincehello.store.store import ME_PERSON_ID

    mine = sg.person_id == ME_PERSON_ID
    mid = midpoint_ms(sg)
    # Their offset is compared with yours then; yours with the default zone (your usual).
    ref = (
        _local(mid, tz)
        if mine
        else TzResolver(store, tz, use_inferred=False).local(ME_PERSON_ID, mid)
    )
    diff = sg.utc_offset_hours - ref.utcoffset().total_seconds() / 3600
    way = "ahead of" if diff > 0 else "behind"
    who, whom = ("you were", "usual") if mine else (f"{name} was", "you")
    if sg.kind == "home":
        lead = f"Looks like {name} lives about {abs(diff):g}h {way} you"
        when = f"(since {_day(sg.start_utc, tz)})"
    else:
        lead = f"Looks like {who} about {abs(diff):g}h {way} {whom}"
        until = f"to {_day(sg.end_utc, tz)}" if sg.end_utc is not None else "onwards"
        when = f"from {_day(sg.start_utc, tz)} {until}"
    text = f"{lead} {when}, {offset_label(sg.utc_offset_hours)}"
    if sg.hint_message_id:
        source, _, msg_id = sg.hint_message_id.partition(":")
        row = store.conn.execute(
            "SELECT is_from_me, sent_at_utc FROM message WHERE source = ? AND source_msg_id = ?",
            (source, msg_id),
        ).fetchone()
        if row is not None:
            speaker = "you" if row["is_from_me"] else name
            text += (
                f" ({speaker} said \u201c{sg.hint_quote}\u201d on {_day(row['sent_at_utc'], tz)})"
            )
    zone = f"{sg.iana_tz} ({sg.place})" if sg.place else sg.iana_tz
    return f"{text}.\n  Suggested zone: {zone}. Confidence: {sg.confidence}."


def _print_timeline(store: Store, person_id: int, name: str, tz: str) -> None:
    from sincehello.analysis.tz import TzResolver
    from sincehello.store.store import ME_PERSON_ID

    resolver = TzResolver(store, tz)
    for pid, label in ((ME_PERSON_ID, "you"), (person_id, name)):
        periods = resolver.timeline(pid)
        typer.echo(f"{label}: {tz} by default" + (", plus:" if periods else ""))
        for p in periods:
            end = p["end_utc"][:10] if p["end_utc"] else "open"
            typer.echo(f"  {p['start_utc'][:10]} -> {end:<10}  {p['tz']:<22} {p['source']}")


@tz_app.command("show")
def tz_show(
    person_id: Annotated[int, typer.Argument(help="Person id, from `sincehello people`.")],
    tz: TzOption = None,
) -> None:
    """Your and their timezone periods, and suggestions waiting for an answer."""
    from sincehello.analysis.tz_infer import pending_suggestions

    tz = tz or config.default_timezone()
    with Store(config.store_path()) as store:
        name = _person_name(store, person_id)
        _print_timeline(store, person_id, name, tz)
        pending = pending_suggestions(store, person_id)
        if pending:
            typer.echo(f"\n{len(pending)} suggestions waiting (sincehello tz confirm {person_id}):")
        for sg in pending:
            typer.echo(f"  [{sg.id}] {_describe(store, sg, name, tz)}")


@tz_app.command("infer")
def tz_infer_cmd(
    person_id: Annotated[int, typer.Argument(help="Person id, from `sincehello people`.")],
    tz: TzOption = None,
    name: Annotated[
        bool, typer.Option("--name/--no-name", help="Ask Claude to name each place (costs money).")
    ] = False,
    max_cost: Annotated[float, typer.Option(help="Hard spending cap for --name, USD.")] = 0.25,
    model: Annotated[str, typer.Option(help="Model for --name.")] = "claude-opus-5-5",
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Find timezone shifts from sleep windows (local), optionally name them (Claude)."""
    from sincehello.analysis import tz_infer

    tz = tz or config.default_timezone()
    with Store(config.store_path()) as store:
        who = _person_name(store, person_id)
        pending = tz_infer.sync(store, person_id, default_tz=tz)
        if name and pending:
            _name_suggestions(store, person_id, pending, max_cost, model)
            pending = tz_infer.pending_suggestions(store, person_id)
        if as_json:
            out = {"person_id": person_id, "suggestions": [sg.to_dict() for sg in pending]}
            typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
            return
        if not pending:
            typer.echo("No timezone shifts to suggest.")
        for sg in pending:
            typer.echo(f"[{sg.id}] {_describe(store, sg, who, tz)}")
        if pending:
            typer.echo(f"Review them with `sincehello tz confirm {person_id}`.")


def _name_suggestions(store: Store, person_id: int, pending, max_cost: float, model: str) -> None:
    from datetime import UTC, datetime

    from sincehello.agent.budget import Budget
    from sincehello.agent.context import ToolContext
    from sincehello.agent.tz_namer import NamerConfig, NameUnavailable, TzNamer

    budget = Budget(max_cost)
    namer = TzNamer(_anthropic_client(), budget, NamerConfig(model=model))
    ctx = ToolContext(store, person_id)
    for sg in pending:
        if sg.place is not None:
            continue
        try:
            got = namer(sg, ctx)
        except NameUnavailable as e:
            typer.echo(f"[{sg.id}] not named: {e}", err=True)
            continue
        for why in got.dropped:
            typer.echo(f"[{sg.id}] dropped: {why}", err=True)
        if got.iana_tz is None:
            typer.echo(f"[{sg.id}] no place found ({got.notes})", err=True)
            continue
        fields = {"place": got.place, "iana_tz": got.iana_tz}
        if got.evidence_message_id:
            fields |= {"hint_message_id": got.evidence_message_id, "hint_quote": got.quote}
        store.update_tz_suggestion(sg.id, **fields)
    egress = ctx.egress.summary(len(ctx.messages))
    path = config.home() / "tz" / f"name-{person_id}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**egress, "entries": ctx.egress.entries}, indent=2))
    typer.echo(
        f"naming: {budget.critic_calls} calls, ${budget.spent:.4f},"
        f" {egress['messages_shared']} messages shared ({path})",
        err=True,
    )


def _parse_local(text: str, tz: str) -> int | None:
    """ "open" -> None; "YYYY-MM-DD" or "YYYY-MM-DD HH:MM" in `tz` -> UTC ms."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    text = text.strip()
    if text.lower() in ("open", "none", ""):
        return None
    dt = datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz))
    return int(dt.timestamp() * 1000)


@tz_app.command("confirm")
def tz_confirm(
    person_id: Annotated[int, typer.Argument(help="Person id, from `sincehello people`.")],
    tz: TzOption = None,
    everything: Annotated[
        bool, typer.Option("--all", help="Also ask about low-confidence suggestions.")
    ] = False,
) -> None:
    """Accept, edit or ignore each inferred period. Accepted ones go to tz_period."""
    from zoneinfo import ZoneInfoNotFoundError

    from sincehello.analysis import tz_infer

    tz = tz or config.default_timezone()
    with Store(config.store_path()) as store:
        who = _person_name(store, person_id)
        pending = tz_infer.sync(store, person_id, default_tz=tz)
        ask = [sg for sg in pending if everything or sg.confidence != "low"]
        if not ask:
            low = len(pending) - len(ask)
            extra = f" ({low} low-confidence; --all to see them)" if low else ""
            typer.echo(f"Nothing to confirm{extra}.")
            return
        for n, sg in enumerate(ask, 1):
            typer.echo(f"\n[{n}/{len(ask)}] {_describe(store, sg, who, tz)}")
            while True:
                answer = typer.prompt("correct, edit, ignore, skip or quit? [c/e/i/s/q]").strip()
                answer = answer.lower()[:1]
                if answer in ("c", "e", "i", "s", "q"):
                    break
            if answer == "q":
                break
            if answer == "s":
                continue
            if answer == "i":
                tz_infer.ignore(store, sg)
                typer.echo("Ignored.")
                continue
            if answer == "c":
                tz_infer.accept(store, sg)
                typer.echo(f"Saved: {sg.iana_tz}.")
                continue
            fmt = "%Y-%m-%d %H:%M"
            end_default = _local(sg.end_utc, tz).strftime(fmt) if sg.end_utc is not None else "open"
            while True:
                zone = typer.prompt("zone (IANA, e.g. Asia/Tokyo)", default=sg.iana_tz)
                start = typer.prompt(
                    f"start ({tz})", default=_local(sg.start_utc, tz).strftime(fmt)
                )
                end = typer.prompt(f"end ({tz}, or open)", default=end_default)
                try:
                    start_ms = _parse_local(start, tz)
                    if start_ms is None:
                        raise ValueError("a period needs a start")
                    tz_infer.accept(
                        store, sg, iana_tz=zone, start_utc=start_ms, end_utc=_parse_local(end, tz)
                    )
                except (ValueError, ZoneInfoNotFoundError) as e:
                    typer.echo(f"Can't save that: {e}")
                    continue
                typer.echo(f"Saved: {zone}.")
                break


synth = typer.Typer(help="Synthetic histories with planted facts for evals (SPEC.md §10.1).")
app.add_typer(synth, name="synth")


@synth.command("list")
def synth_list() -> None:
    """List the scenarios in evals/scenarios/."""
    from evals.generator import scenario

    for name in scenario.available():
        typer.echo(f"{name:<16} {scenario.load(name).description}")


@synth.command("generate")
def synth_generate(
    scenario_id: Annotated[str, typer.Argument(help="Scenario id or path to a JSON spec.")],
    seed: Annotated[int, typer.Option(help="RNG seed; same seed, same history.")] = 0,
    out: Annotated[
        Path | None, typer.Option(help="Output directory (default ./synthetic/<history id>).")
    ] = None,
    messages: Annotated[
        int | None, typer.Option(help="Approximate total lines (default: the scenario's).")
    ] = None,
    llm: Annotated[
        bool, typer.Option(help="Rewrite filler text with Claude (costs money).")
    ] = False,
    model: Annotated[str, typer.Option(help="Model for --llm.")] = "claude-opus-5-5",
    effort: Annotated[str, typer.Option(help="Effort for --llm.")] = "medium",
    max_chunks: Annotated[
        int | None, typer.Option(help="With --llm, rewrite at most this many chunks.")
    ] = None,
) -> None:
    """Generate one history: messages.jsonl + manifest.json."""
    from evals import generator
    from evals.generator import scenario as scenario_mod

    try:
        sc = scenario_mod.load(scenario_id)
    except FileNotFoundError:
        typer.echo(f"unknown scenario {scenario_id!r} (see `sincehello synth list`)", err=True)
        raise typer.Exit(1) from None
    records, manifest = generator.generate(sc, seed, messages)
    if llm:
        from evals.generator import llm as llm_mod

        records, manifest, report = llm_mod.rewrite(
            sc, records, manifest, model=model, effort=effort, max_chunks=max_chunks
        )
        typer.echo(
            f"llm: {report.rewritten} lines rewritten, {report.rejected} kept offline,"
            f" {report.requests} requests, {report.input_tokens} in / {report.output_tokens}"
            " out tokens"
        )
        for note in report.notes:
            typer.echo(f"note: {note}", err=True)
    out = out or Path("synthetic") / manifest["history_id"]
    generator.write(out, records, manifest)
    s = manifest["stats"]
    typer.echo(
        f"{manifest['history_id']}: {s['lines']} lines ({s['dm_messages']} 1:1,"
        f" {s['dm_reactions']} reactions, {s['group_messages']} group),"
        f" {len(manifest['facts'])} planted facts -> {out}"
    )


def _anthropic_client():
    """The real API client. Tests replace this with a scripted fake."""
    import anthropic

    return anthropic.Anthropic()


EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _check_effort(effort: str) -> None:
    if effort not in EFFORTS:
        typer.echo(f"--effort must be one of {', '.join(EFFORTS)}", err=True)
        raise typer.Exit(1)


CriticFlag = Annotated[
    bool, typer.Option("--critic/--no-critic", help="Review each insight with the LLM critic.")
]
CriticModel = Annotated[str, typer.Option(help="Model for the critic.")]
CriticEffort = Annotated[str, typer.Option(help="Critic effort: low | medium | high | ...")]


def _critic(enabled: bool, client, model: str, effort: str):
    if not enabled:
        return None
    from sincehello.verify.critic import CriticConfig, LLMCritic

    _check_effort(effort)
    return LLMCritic(client, CriticConfig(model=model, effort=effort))


DeckSize = Annotated[
    int, typer.Option(help="Most cards in the deck (SPEC.md §7.7); at least 12 when available.")
]


def _deck_config(size: int):
    from sincehello.curate import CurationConfig

    if size < 1:
        typer.echo("--deck-size must be at least 1", err=True)
        raise typer.Exit(1)
    return CurationConfig(min_cards=min(12, size), max_cards=size)


def _print_run(result) -> None:
    run = result.report.run
    typer.echo(
        f"{run.status}: {len(result.report.insights)} insights in {run.turns} turns,"
        f" ${run.cost_usd:.4f}, {run.messages_shared} messages shared ({run.pct_shared}%)"
    )
    if run.critic_calls:
        verdicts: dict[str, int] = {}
        for ins in result.report.insights:
            verdicts[ins.verification.critic] = verdicts.get(ins.verification.critic, 0) + 1
        shown = ", ".join(f"{n} {v}" for v, n in sorted(verdicts.items())) or "none"
        typer.echo(f"critic: {run.critic_calls} calls, ${run.critic_cost_usd:.4f}; kept {shown}")
    _print_deck(result.report)
    for note in result.notes:
        typer.echo(f"note: {note}", err=True)
    typer.echo(f"run output: {result.run_dir}")


@app.command()
def analyze(
    person_id: Annotated[int, typer.Argument(help="Person id, from `sincehello people`.")],
    max_cost: Annotated[float, typer.Option(help="Hard spending cap for this run, USD.")] = 5.0,
    model: Annotated[str, typer.Option(help="Model for the discovery agent.")] = "claude-opus-5-5",
    effort: Annotated[str, typer.Option(help="low | medium | high | xhigh | max.")] = "high",
    max_turns: Annotated[int, typer.Option(help="Turn cap for the agent loop.")] = 60,
    tz: Annotated[str | None, typer.Option(help="Timezone for people with no tz periods.")] = None,
    critic: CriticFlag = True,
    critic_model: CriticModel = "claude-opus-5-5",
    critic_effort: CriticEffort = "medium",
    deck_size: DeckSize = 20,
) -> None:
    """Run the discovery agent (Claude) on one person. Writes ~/.sincehello/runs/<run_id>/."""
    from sincehello.agent import AgentConfig, run_agent
    from sincehello.agent.runlog import new_run_id, runs_dir

    _check_effort(effort)
    _check_effort(critic_effort)
    config_ = AgentConfig(
        model=model,
        effort=effort,
        max_cost=max_cost,
        max_turns=max_turns,
        default_tz=tz,
        deck=_deck_config(deck_size),
    )
    with Store(config.store_path()) as store:
        if person_id not in {p.person_id for p in store.people()}:
            typer.echo(f"unknown person {person_id} (see `sincehello people`)", err=True)
            raise typer.Exit(1)
        _tz_note(store, person_id, tz)
        run_dir = runs_dir() / new_run_id(person_id)
        client = _anthropic_client()
        reviewer = _critic(critic, client, critic_model, critic_effort)
        result = run_agent(store, person_id, client, config_, run_dir, critic=reviewer)
    _print_run(result)


def _tz_note(store: Store, person_id: int, tz: str | None) -> None:
    """Refresh timezone suggestions (local, no LLM) and say if any wait for the user."""
    from sincehello.analysis import tz_infer

    pending = tz_infer.sync(store, person_id, default_tz=tz)
    asked = [sg for sg in pending if sg.confidence != "low"]
    if not asked:
        return
    used = sum(sg.confidence == "high" for sg in asked)
    typer.echo(
        f"timezones: {len(asked)} inferred periods to review (`sincehello tz confirm"
        f" {person_id}`); {used} high-confidence ones are used, labeled inferred, until then.",
        err=True,
    )


eval_app = typer.Typer(help="Evals (SPEC.md §10). These call Claude and cost money.")
app.add_typer(eval_app, name="eval")


@eval_app.command("agent")
def eval_agent(
    sample: Annotated[str, typer.Option(help="Sample under evals/samples/.")] = "bean-goose",
    max_cost: Annotated[float, typer.Option(help="Hard spending cap, USD.")] = 5.0,
    model: Annotated[str, typer.Option(help="Model for the discovery agent.")] = "claude-opus-5-5",
    effort: Annotated[str, typer.Option(help="low | medium | high | xhigh | max.")] = "high",
    max_turns: Annotated[int, typer.Option(help="Turn cap for the agent loop.")] = 60,
    critic: CriticFlag = True,
    critic_model: CriticModel = "claude-opus-5-5",
    critic_effort: CriticEffort = "medium",
) -> None:
    """Run the agent on a synthetic sample and score recall of its planted facts."""
    import tempfile

    from evals import agent_recall, generator
    from sincehello.agent import AgentConfig, run_agent
    from sincehello.agent.runlog import new_run_id, runs_dir
    from sincehello.connectors.synthetic import SAMPLE_DIR, SyntheticConnector

    _check_effort(effort)
    _check_effort(critic_effort)
    path = SAMPLE_DIR.parent / sample
    if not (path / "manifest.json").is_file():
        typer.echo(f"no sample {sample!r} in {SAMPLE_DIR.parent}", err=True)
        raise typer.Exit(1)
    records, manifest = generator.read(path)
    config_ = AgentConfig(model=model, effort=effort, max_cost=max_cost, max_turns=max_turns)
    with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp) / "eval.db") as store:
        SyntheticConnector(path).ingest(store)
        person_id = store.person_for_handle("synthetic", manifest["participants"]["them"]["handle"])
        run_dir = runs_dir() / f"{new_run_id(person_id)}-eval-{sample}"
        client = _anthropic_client()
        reviewer = _critic(critic, client, critic_model, critic_effort)
        result = run_agent(store, person_id, client, config_, run_dir, critic=reviewer)
    insights = [i.model_dump() for i in result.report.insights]
    scored = agent_recall.score(manifest, records, insights)
    (run_dir / "eval.json").write_text(json.dumps(scored, indent=2))
    _print_run(result)
    for f in scored["facts"]:
        mark = "x" if f["found"] else " "
        typer.echo(f"  [{mark}] {f['id']:<16} {f['type']:<16} {', '.join(f['insights'])}")
    typer.echo(f"recall {scored['found']}/{scored['total']} ({scored['recall']:.0%})")


@eval_app.command("tz")
def eval_tz(
    sample: Annotated[str | None, typer.Option(help="Sample under evals/samples/.")] = None,
    scenario: Annotated[
        str | None, typer.Option(help="Generate this scenario instead of using a sample.")
    ] = None,
    seed: Annotated[
        list[int] | None, typer.Option(help="Seeds for --scenario (repeatable).")
    ] = None,
    messages: Annotated[int | None, typer.Option(help="Lines per generated history.")] = None,
) -> None:
    """Recall of planted timezone periods by inference (SPEC.md §6.1). Free, no API calls."""
    import tempfile

    from evals import generator, tz_recall
    from sincehello.connectors.synthetic import SAMPLE_DIR

    results = []
    if scenario is None:
        path = SAMPLE_DIR.parent / (sample or "bean-goose")
        if not (path / "manifest.json").is_file():
            typer.echo(f"no sample {sample!r} in {SAMPLE_DIR.parent}", err=True)
            raise typer.Exit(1)
        results.append(tz_recall.run(path))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            for n in seed or [0]:
                records, manifest = generator.generate(scenario, n, messages)
                out = Path(tmp) / manifest["history_id"]
                generator.write(out, records, manifest)
                results.append(tz_recall.run(out))
    found = total = 0
    for r in results:
        found, total = found + r["found"], total + r["total"]
        typer.echo(f"{r['history_id']}: {r['found']}/{r['total']}")
        for e in r["expected"]:
            mark = "x" if e["found"] else " "
            end = e["end_error_days"]
            detail = (
                f"start {e['start_error_days']:+g}d end {'open' if end is None else f'{end:+g}d'}"
                f" offset {e['offset']:+g} ({e['confidence']})"
                if e["found"]
                else ""
            )
            typer.echo(f"  [{mark}] {e['who']:<4} {e['tz']:<20} {e['start'][:10]}  {detail}")
        for t in r["trap_hits"]:
            typer.echo(f"  trap suggested: {t['who']} {t['tz']} {t['start'][:10]}")
        for fp in r["false_positives"]:
            typer.echo(
                f"  false positive: person {fp['person_id']} {fp['start'][:10]}"
                f" {fp['utc_offset_hours']:+g} ({fp['confidence']})"
            )
    if total:
        typer.echo(f"recall {found}/{total} ({found / total:.0%})")


@app.command()
def merge(
    person: Annotated[str, typer.Argument(help="Person id, or any handle of that person.")],
    handle: Annotated[str, typer.Argument(help="Handle to move: raw value or handle id.")],
) -> None:
    """Move a handle to a person (manual identity merge)."""
    with Store(config.store_path()) as store:
        try:
            person_id = identity.find_person(store, person)
            h = identity.find_handle(store, handle)
            left = identity.merge_handle(store, person_id, h.id)
        except identity.IdentityError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
    if left is None:
        typer.echo(f"{h.raw_value} already belongs to person {person_id}.")
    else:
        typer.echo(f"Moved {h.raw_value} from person {left} to person {person_id}.")


@app.command()
def me(
    handle: Annotated[str, typer.Argument(help="Your own handle: raw value or handle id.")],
) -> None:
    """Mark a handle as your own, so it drops out of `people`."""
    with Store(config.store_path()) as store:
        try:
            h = identity.find_handle(store, handle)
            identity.mark_me(store, h.id)
        except identity.IdentityError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
    typer.echo(f"Marked {h.raw_value} as yours.")


@eval_app.command("critic")
def eval_critic(
    max_cost: Annotated[float, typer.Option(help="Hard spending cap, USD.")] = 0.50,
    critic_model: CriticModel = "claude-opus-5-5",
    critic_effort: CriticEffort = "medium",
) -> None:
    """Run the critic on hand-labelled insights about bean-goose; report agreement."""
    from datetime import UTC, datetime

    from evals import critic_calibration

    _check_effort(critic_effort)
    reviewer = _critic(True, _anthropic_client(), critic_model, critic_effort)
    out = critic_calibration.run(reviewer, max_cost=max_cost)
    path = config.home() / "evals" / f"critic-calibration-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    for r in out["cases"]:
        mark = "=" if r["agree"] else ("~" if r["acceptable"] else "x")
        got = r["verdict"] or f"none ({r['error']})"
        s = r["scores"] or {}
        scores = " ".join(f"{k[0]}{v}" for k, v in s.items())
        typer.echo(f"  [{mark}] {r['case']:<22} label {r['label']:<7} got {got:<7} {scores}")
    agree = sum(r["agree"] for r in out["cases"])
    typer.echo(
        f"agreement {agree}/{out['judged']} (acceptable {out['acceptable']},"
        f" pass-vs-flag {out['pass_vs_flag']}); {out['calls']} calls, ${out['cost_usd']:.4f}"
    )
    typer.echo(f"results: {path}")


# --- M6 eval harness: LLM judge, human ratings, calibration, comparison (SPEC.md §10.2-10.4)

JudgeModel = Annotated[str, typer.Option(help="Model for the eval judge.")]
JudgeEffort = Annotated[str, typer.Option(help="Judge effort: low | medium | high | ...")]


def _judge(max_cost: float, model: str, effort: str, log=None):
    from evals.judge import JudgeConfig, LLMJudge
    from sincehello.agent.budget import Budget

    _check_effort(effort)
    return LLMJudge(
        _anthropic_client(), Budget(max_cost), JudgeConfig(model=model, effort=effort), log
    )


def _run_dir(run: str) -> Path:
    from evals import runs

    try:
        return runs.resolve(run)
    except runs.RunError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


def _scores(s: dict | None) -> str:
    return " ".join(f"{k[:4]} {v}" for k, v in (s or {}).items())


@eval_app.command("judge")
def eval_judge(
    run: Annotated[str, typer.Argument(help="Run id (under ~/.sincehello/runs/) or run dir.")],
    sample: Annotated[
        str | None,
        typer.Option(help="Synthetic sample the run used (default: from an -eval-<sample> id)."),
    ] = None,
    max_cost: Annotated[float, typer.Option(help="Hard spending cap, USD.")] = 2.0,
    judge_model: JudgeModel = "claude-opus-5-5",
    judge_effort: JudgeEffort = "medium",
) -> None:
    """Score every insight in a run with the LLM judge. Writes <run>/judge.json."""
    from evals import runs
    from evals.judge.judge import judge_insights

    run_dir = _run_dir(run)
    report = runs.load_report(run_dir)
    with runs.event_log(run_dir / "judge_log.jsonl") as log:
        judge = _judge(max_cost, judge_model, judge_effort, log)
        try:
            with runs.context(run_dir, report, sample) as ctx:
                out = judge_insights(judge, report.insights, ctx, runs.queries(run_dir, report))
        except runs.RunError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from None
    out["run_id"] = run_dir.name
    (run_dir / "judge.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    for r in out["insights"]:
        mark = {True: "+", False: "-"}.get(r["keep"], "?")
        shown = _scores(r["scores"]) if r["scores"] else f"none ({r['error']})"
        typer.echo(f"  [{mark}] {r['insight']:<10} {r['critic']:<8} {shown}  {r['title']}")
    m = out["means"] or {}
    typer.echo(
        f"judged {out['scored']}/{len(out['insights'])}: overall {m.get('overall')},"
        f" keep {out['keep_rate']}, hallucination {out['hallucination_rate']};"
        f" {out['calls']} calls, ${out['cost_usd']:.4f},"
        f" {out['egress']['messages_shown']} messages shown"
    )
    typer.echo(f"results: {run_dir / 'judge.json'}")


@eval_app.command("rate")
def eval_rate(
    run: Annotated[str, typer.Argument(help="Run id (under ~/.sincehello/runs/) or run dir.")],
    everything: Annotated[
        bool, typer.Option("--all", help="Rate every insight, not only the deck.")
    ] = False,
    redo: Annotated[bool, typer.Option(help="Ask again for cards already rated.")] = False,
) -> None:
    """Rate a run's insights yourself (SPEC.md §10.3). Saves to <run>/ratings.json."""
    from evals import ratings, runs
    from evals.judge import DIMENSIONS

    run_dir = _run_dir(run)
    report = runs.load_report(run_dir)
    done = ratings.load(run_dir)
    todo = [i for i in ratings.to_rate(report, everything) if redo or i.id not in done]
    typer.echo(
        f"{len(todo)} to rate, {len(done)} already rated. Scores 1-5 for:"
        f" {', '.join(DIMENSIONS)} (e.g. 5 4 4 5 3). s skips, q quits."
    )
    for n, ins in enumerate(todo, 1):
        typer.echo(f"\n[{n}/{len(todo)}] {ins.id}  {ins.category}/{ins.kind}")
        typer.echo(f"  {ins.title}\n  {ins.headline}")
        if ins.body:
            typer.echo(f"  {ins.body}")
        for ev in ins.evidence:
            typer.echo(f'    > "{ev.quote}"  ({ev.message_id})')
        while True:
            answer = typer.prompt("scores").strip().lower()
            if answer in ("s", "q"):
                break
            try:
                scores = ratings.parse_scores(answer)
                break
            except ValueError as e:
                typer.echo(str(e))
        if answer == "q":
            break
        if answer == "s":
            continue
        keep = typer.confirm("would you want to see this?")
        note = typer.prompt("note", default="", show_default=False)
        done[ins.id] = ratings.Rating(scores=scores, keep=keep, note=note)
        ratings.save(run_dir, done)
    typer.echo(f"\n{len(done)} rated -> {ratings.path(run_dir)}")


@eval_app.command("agreement")
def eval_agreement(
    run_ids: Annotated[list[str], typer.Argument(help="Runs with judge.json and ratings.json.")],
) -> None:
    """Agreement between the judge and your ratings, pooled over runs (no API calls)."""
    from evals import ratings, runs
    from evals.judge.agreement import agreement

    judged: dict[str, dict] = {}
    human: dict[str, dict] = {}
    for run in run_ids:
        run_dir = _run_dir(run)
        jd = runs.read_json(run_dir, "judge.json")
        if jd is None:
            typer.echo(f"{run_dir.name}: no judge.json (run `sincehello eval judge`)", err=True)
            raise typer.Exit(1)
        for r in jd["insights"]:
            judged[f"{run_dir.name}/{r['insight']}"] = r
        for k, v in ratings.as_rows(ratings.load(run_dir)).items():
            human[f"{run_dir.name}/{k}"] = v
    out = agreement(judged, human)
    _print_agreement(out)


def _print_agreement(out: dict) -> None:
    typer.echo(f"{out['n']} insights scored by both")
    for d, v in out["dimensions"].items():
        typer.echo(
            f"  {d:<16} n {v['n']:>3}  exact {v['exact']:.0%}  within 1 {v['within_1']:.0%}"
            f"  mae {v['mae']}  bias {v['bias']:+}"
        )
    k = out["keep"]
    typer.echo(f"  keep             n {k['n']:>3}  agreement {k['agreement']}")


@eval_app.command("judge-calibration")
def eval_judge_calibration(
    max_cost: Annotated[float, typer.Option(help="Hard spending cap, USD.")] = 1.0,
    judge_model: JudgeModel = "claude-opus-5-5",
    judge_effort: JudgeEffort = "medium",
) -> None:
    """Run the judge on hand-labelled insights about bean-goose; report agreement."""
    from datetime import UTC, datetime

    from evals import judge_calibration, runs

    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    base = config.home() / "evals"
    with runs.event_log(base / f"judge-calibration-{stamp}.jsonl") as log:
        out = judge_calibration.run(_judge(max_cost, judge_model, judge_effort, log))
    path = base / f"judge-calibration-{stamp}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    for r in out["insights"]:
        label = judge_calibration.LABELS[r["insight"].removeprefix("cal_")]
        got = _scores(r["scores"]) if r["scores"] else f"none ({r['error']})"
        typer.echo(f"  {r['insight']:<26} keep {r['keep']!s:<5} (label {label.keep!s:<5}) {got}")
    _print_agreement(out["agreement"])
    typer.echo(f"{out['calls']} calls, ${out['cost_usd']:.4f}; results: {path}")


@eval_app.command("compare")
def eval_compare(
    run_a: Annotated[str, typer.Argument(help="Baseline run.")],
    run_b: Annotated[str, typer.Argument(help="Candidate run.")],
    pairwise: Annotated[
        bool, typer.Option(help="Also ask the judge which deck is better (costs money).")
    ] = False,
    max_cost: Annotated[float, typer.Option(help="Hard spending cap for --pairwise, USD.")] = 0.5,
    seed: Annotated[int | None, typer.Option(help="Seed for the deck order.")] = None,
    judge_model: JudgeModel = "claude-opus-5-5",
    judge_effort: JudgeEffort = "medium",
) -> None:
    """Two runs side by side: recall, judge scores, ratings, cost (SPEC.md §10.4)."""
    import random

    from evals import runs
    from evals.judge import JudgeUnavailable

    a_dir, b_dir = _run_dir(run_a), _run_dir(run_b)
    a, b = runs.summary(a_dir), runs.summary(b_dir)
    rows = [
        ("model / effort", lambda s: f"{s['model']} / {s['effort']}"),
        ("status", lambda s: s["status"]),
        ("cost", lambda s: f"${s['cost_usd']:.4f}"),
        ("turns", lambda s: s["turns"]),
        ("insights / pass", lambda s: f"{s['insights']} / {s['critic_pass']}"),
        ("recall", lambda s: s.get("recall")),
        ("judge overall", lambda s: (s.get("judge") or {}).get("overall")),
        ("judge keep", lambda s: (s.get("judge") or {}).get("keep_rate")),
        ("hallucination", lambda s: (s.get("judge") or {}).get("hallucination_rate")),
        ("human keep", lambda s: (s.get("human") or {}).get("keep_rate")),
    ]
    typer.echo(f"{'':<18} {'A ' + a['run_id']:<40} B {b['run_id']}")
    for name, get in rows:
        typer.echo(f"{name:<18} {get(a)!s:<40} {get(b)!s}")
    va, vb = (
        (a.get("judge") or {}).get("prompt_version"),
        (b.get("judge") or {}).get("prompt_version"),
    )
    if va and vb and va != vb:
        typer.echo("note: judged with different judge prompts; scores aren't comparable", err=True)
    if not pairwise:
        return
    ra, rb = runs.load_report(a_dir), runs.load_report(b_dir)
    if ra.person.get("name") != rb.person.get("name"):
        typer.echo("note: the runs are about different people", err=True)
    with runs.event_log(b_dir / "judge_log.jsonl") as log:
        judge = _judge(max_cost, judge_model, judge_effort, log)
        who = {"me": None, "them": rb.person.get("name")}
        try:
            res = judge.compare(who, runs.deck(ra), runs.deck(rb), random.Random(seed))
        except JudgeUnavailable as e:
            typer.echo(f"pairwise: no result ({e})", err=True)
            raise typer.Exit(1) from None
    out = {"a": a_dir.name, "b": b_dir.name, **res.to_dict(), "cost_usd": judge.budget.spent}
    path = b_dir / f"pairwise-vs-{a_dir.name}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    winner = {"a": f"A ({a_dir.name})", "b": f"B ({b_dir.name})", "tie": "tie"}[res.winner]
    typer.echo(f"pairwise: {winner}, A shown {'first' if res.shown_first == 'a' else 'second'}")
    typer.echo(f"  {res.reasons}")
    typer.echo(f"${judge.budget.spent:.4f}; results: {path}")


def _print_deck(report) -> None:
    by_id = {i.id: i for i in report.insights}
    cur = report.curation
    label = "" if cur is None or cur.verified else " (unverified: no critic ran)"
    typer.echo(f"deck: {len(report.deck)} cards{label}")
    for n, iid in enumerate(report.deck, 1):
        ins = by_id[iid]
        typer.echo(f"  {n:>2}. {iid} {ins.category:<9} {ins.title}")


@app.command()
def curate(
    run_dir: Annotated[Path, typer.Argument(help="A run directory with a report.json.")],
    deck_size: DeckSize = 20,
) -> None:
    """Re-curate an existing run's report.json in place (SPEC.md §7.7). No LLM calls."""
    from sincehello.curate import curate as curate_report
    from sincehello.report.schema import Report

    path = run_dir / "report.json"
    if not path.is_file():
        typer.echo(f"no report.json in {run_dir}", err=True)
        raise typer.Exit(1)
    report = curate_report(Report.model_validate_json(path.read_text()), _deck_config(deck_size))
    path.write_text(report.model_dump_json(indent=2))
    _print_deck(report)
