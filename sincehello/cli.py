"""Command-line interface (SPEC.md §11)."""

from __future__ import annotations

from typing import Annotated

import typer

from sincehello import config
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
