"""Command-line interface (SPEC.md §11)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from sincehello import config
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
