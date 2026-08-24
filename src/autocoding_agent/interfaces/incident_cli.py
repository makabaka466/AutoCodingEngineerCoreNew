"""CLI adapter for the incident investigation workflow."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from autocoding_agent.adapters.sqlite_database import SQLiteDatabaseReader
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import IncidentOutcome

app = typer.Typer(
    name="autocoding-incident",
    help="Locate an affected page and diagnose an incident with read-only data.",
    no_args_is_help=True,
)


@app.command()
def start(
    problem: Annotated[str, typer.Argument(help="Observed problem or exception.")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w", help="Application source root.")],
    page: Annotated[
        str | None,
        typer.Option("--page", "-p", help="Page route, URL, title, or module hint."),
    ] = None,
    database: Annotated[
        Path | None,
        typer.Option("--database", "-d", help="Read-only SQLite database path."),
    ] = None,
) -> None:
    """Start one incident investigation."""

    _invoke(
        lambda: build_incident_application(sqlite_path=database).start(
            workspace,
            problem,
            page,
        )
    )


@app.command()
def send(
    session_id: Annotated[str, typer.Option("--session-id", "-s")],
    message: Annotated[str, typer.Argument(help="Answer or additional incident context.")],
    database: Annotated[
        Path | None,
        typer.Option("--database", "-d", help="Read-only SQLite database path."),
    ] = None,
) -> None:
    """Continue an incident that requested more input."""

    _invoke(lambda: build_incident_application(sqlite_path=database).send(session_id, message))


@app.command()
def show(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Show the latest durable incident outcome."""

    _invoke(lambda: build_incident_application().outcome(session_id))


@app.command("sessions")
def sessions_command() -> None:
    """List recent incident sessions without opening a database."""

    try:
        sessions = build_incident_application().list_sessions()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            [
                {
                    "session_id": item.id,
                    "status": item.status,
                    "problem": item.problem,
                    "page_hint": item.page_hint,
                    "source": item.source,
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in sessions
            ],
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@app.command("check-db")
def check_database(
    database: Annotated[Path, typer.Option("--database", "-d", help="SQLite database path.")],
) -> None:
    """Validate read-only access and print bounded schema metadata."""

    try:
        typer.echo(SQLiteDatabaseReader(database).describe_schema())
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _invoke(operation: Callable[[], IncidentOutcome]) -> None:
    try:
        outcome = operation()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
