"""Command-line interface (SPEC.md §11)."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from sincehello import config
from sincehello.analysis import compute_all
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
