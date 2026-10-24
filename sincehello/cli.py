"""Command-line interface (SPEC.md §11)."""

from __future__ import annotations

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
