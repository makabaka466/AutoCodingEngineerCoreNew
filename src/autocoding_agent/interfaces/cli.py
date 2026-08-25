"""Command-line adapter for the shared AgentApplication API."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from autocoding_agent.application import build_application
from autocoding_agent.core.models import AgentOutcome
from autocoding_agent.core.recovery.models import RecoveryAction

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
    command_id: Annotated[str | None, typer.Option("--command-id")] = None,
) -> None:
    """Continue a task with another user message."""

    _invoke(lambda: build_application().send(session_id, message, command_id))


@app.command()
def approve(
    session_id: Annotated[str, typer.Option("--session-id", "-s")],
    command_id: Annotated[str | None, typer.Option("--command-id")] = None,
) -> None:
    """Approve the exact modification or verification scope requested by the Agent."""

    _invoke(lambda: build_application().approve(session_id, command_id))


@app.command()
def reject(
    session_id: Annotated[str, typer.Option("--session-id", "-s")],
    reason: Annotated[str, typer.Option("--reason", "-r")] = "",
    command_id: Annotated[str | None, typer.Option("--command-id")] = None,
) -> None:
    """Reject a pending permission request and resume without it."""

    _invoke(lambda: build_application().reject(session_id, reason, command_id))


@app.command()
def show(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Show the latest durable outcome for a task."""

    _invoke(lambda: build_application().outcome(session_id))


@app.command()
def resume(
    session_id: Annotated[str, typer.Option("--session-id", "-s")],
    action: Annotated[
        RecoveryAction,
        typer.Option("--action", "-a", help="read_only_inspect, replan, or cancel"),
    ] = RecoveryAction.READ_ONLY_INSPECT,
) -> None:
    """Resume a paused or recovery-required task using an explicit safe action."""

    _invoke(lambda: build_application().resume(session_id, action))


@app.command()
def pause(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Pause a task at a durable boundary."""

    _invoke(lambda: build_application().pause(session_id))


@app.command()
def cancel(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Cancel a non-terminal task without replaying any action."""

    _invoke(lambda: build_application().cancel(session_id))


@app.command()
def events(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Show the durable task event timeline."""

    _query_json(lambda: build_application().events(session_id))


@app.command()
def artifacts(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Show immutable artifact metadata without printing sensitive file content."""

    _query_json(lambda: build_application().artifacts(session_id))


@app.command()
def runs(session_id: Annotated[str, typer.Option("--session-id", "-s")]) -> None:
    """Show Runtime run leases and terminal results."""

    _query_json(lambda: build_application().runs(session_id))


@app.command()
def explain(
    path: Annotated[str, typer.Argument(help="Workspace-relative file path.")],
    session_id: Annotated[str, typer.Option("--session-id", "-s")],
) -> None:
    """Explain why a file was proposed or changed using decision and artifact metadata."""

    _query_json(lambda: build_application().explain_change(session_id, path))


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
            "task_state": item.task_state,
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


def _query_json(operation: Callable[[], object]) -> None:
    try:
        value = operation()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if isinstance(value, list):
        payload = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value
        ]
    elif hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _print_outcome(outcome: AgentOutcome) -> None:
    typer.echo(f"session: {outcome.session_id}")
    typer.echo(f"status: {outcome.status.value}")
    typer.echo(f"task_state: {outcome.task_state.value}")
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
