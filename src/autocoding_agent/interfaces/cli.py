"""Command-line adapter for the shared AgentApplication API."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from autocoding_agent.application import build_application
from autocoding_agent.core.models import AgentOutcome

app = typer.Typer(
    name="autocoding-agent",
    help="Run and resume software-development Agent tasks.",
    no_args_is_help=True,
)


@app.command()
def start(
    message: Annotated[str, typer.Argument(help="The task to handle.")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w", help="Target repository root.")],
) -> None:
    """Start one new task session."""

    _invoke(lambda: build_application().start(workspace, message))


@app.command()
def send(
    session_id: Annotated[str, typer.Option("--session-id", "-s")],
    message: Annotated[str, typer.Argument(help="Clarification or revised instruction.")],
) -> None:
    """Continue a task with another user message."""

    _invoke(lambda: build_application().send(session_id, message))


@app.command()
def approve(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Approve the exact modification or verification scope requested by the Agent."""

    _invoke(lambda: build_application().approve(session_id))


@app.command()
def reject(
    session_id: Annotated[str, typer.Option("--session-id", "-s")],
    reason: Annotated[str, typer.Option("--reason", "-r")] = "",
) -> None:
    """Reject a pending permission request and resume without it."""

    _invoke(lambda: build_application().reject(session_id, reason))


@app.command()
def show(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Show the latest durable outcome for a task."""

    _invoke(lambda: build_application().outcome(session_id))


@app.command("sessions")
def sessions_command() -> None:
    """List recent task sessions."""

    try:
        sessions = build_application().list_sessions()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    data = [
        {
            "session_id": item.id,
            "status": item.status,
            "workspace": item.workspace,
            "goal": item.goal,
            "updated_at": item.updated_at.isoformat(),
        }
        for item in sessions
    ]
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _invoke(operation: Callable[[], AgentOutcome]) -> None:
    try:
        outcome = operation()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _print_outcome(outcome)


def _print_outcome(outcome: AgentOutcome) -> None:
    typer.echo(f"session: {outcome.session_id}")
    typer.echo(f"status: {outcome.status.value}")
    typer.echo(outcome.message)
    if outcome.approval:
        typer.echo(f"approval: {outcome.approval.scope.value} — {outcome.approval.reason}")
        proposal = outcome.approval.proposal
        if proposal:
            typer.echo(f"proposal: {proposal.summary}")
            for change in proposal.changes:
                location = change.path or change.area
                typer.echo(f"- {location}: {change.current} -> {change.proposed}")
            typer.echo(f"expected: {proposal.expected_result}")
            if proposal.preview_markdown:
                typer.echo(f"preview:\n{proposal.preview_markdown}")
    if outcome.capability_document:
        typer.echo(f"capability: {outcome.capability_document}")


if __name__ == "__main__":
    app()
