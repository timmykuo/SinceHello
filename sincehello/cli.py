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
from sincehello.store import Store

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
) -> None:
    """Run the discovery agent (Claude) on one person. Writes ~/.sincehello/runs/<run_id>/."""
    from sincehello.agent import AgentConfig, run_agent
    from sincehello.agent.runlog import new_run_id, runs_dir

    _check_effort(effort)
    _check_effort(critic_effort)
    config_ = AgentConfig(
        model=model, effort=effort, max_cost=max_cost, max_turns=max_turns, default_tz=tz
    )
    with Store(config.store_path()) as store:
        if person_id not in {p.person_id for p in store.people()}:
            typer.echo(f"unknown person {person_id} (see `sincehello people`)", err=True)
            raise typer.Exit(1)
        run_dir = runs_dir() / new_run_id(person_id)
        client = _anthropic_client()
        reviewer = _critic(critic, client, critic_model, critic_effort)
        result = run_agent(store, person_id, client, config_, run_dir, critic=reviewer)
    _print_run(result)


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
