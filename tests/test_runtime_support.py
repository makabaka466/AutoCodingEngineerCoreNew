"""Direct tests for shared Runtime lifecycle and SQLite infrastructure."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocoding_agent.adapters.sqlite_incident_store import SQLiteIncidentStore
from autocoding_agent.adapters.sqlite_task_store import SQLiteTaskStore
from autocoding_agent.core.models import AgentSession, AgentUsage, EventType
from autocoding_agent.core.progress import ProgressWorkflow
from autocoding_agent.core.runtime.models import (
    RunStatus,
    RuntimeActivity,
    RuntimeEventKind,
)
from autocoding_agent.core.runtime_lifecycle import RuntimeLifecycle, merge_usage


def test_runtime_lifecycle_records_activity_test_and_terminal_event(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="Test shared lifecycle.")
    saves: list[str] = []
    lifecycle = RuntimeLifecycle(
        workflow=ProgressWorkflow.DEVELOPMENT,
        owner_id="test-owner",
        save=lambda aggregate: saves.append(aggregate.id),
        record_test_commands=True,
    )

    run = lifecycle.start(session, mode="verify", command_id="command-1")
    activity = RuntimeActivity(
        run_id=run.id,
        kind=RuntimeEventKind.TOOL_FINISHED,
        summary="pytest completed",
        tool_name="Bash",
        data={"command": "python -m pytest -q", "is_error": False},
    )
    lifecycle.record_activity(
        session,
        run,
        activity,
        command_id="command-1",
        mode="verify",
        progress_sink=None,
    )
    lifecycle.finish(
        session,
        run,
        status=RunStatus.COMPLETED,
        reason=None,
        command_id="command-1",
    )

    assert saves == [session.id]
    assert run.status == RunStatus.COMPLETED
    assert run.activity_ids == [activity.id]
    assert [event.type for event in session.events] == [
        EventType.RUNTIME_STARTED,
        EventType.TOOL_FINISHED,
        EventType.TEST_EXECUTED,
        EventType.RUNTIME_COMPLETED,
    ]


def test_merge_usage_adds_all_provider_totals() -> None:
    merged = merge_usage(
        AgentUsage(input_tokens=10, output_tokens=3, cost_usd=0.1, turns=1),
        AgentUsage(input_tokens=7, output_tokens=5, cost_usd=0.2, turns=2),
    )

    assert merged.input_tokens == 17
    assert merged.output_tokens == 8
    assert merged.cost_usd == pytest.approx(0.3)
    assert merged.turns == 3


def test_both_domain_stores_share_one_consistent_sqlite_connection(tmp_path: Path) -> None:
    root = tmp_path / "data"
    development = SQLiteTaskStore(root, migrate_legacy_json=False)
    incident = SQLiteIncidentStore(root, migrate_legacy_json=False)

    assert development.path == incident.path
    with development.database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {"tasks", "events", "incident_tasks", "incident_events"} <= tables

    with pytest.raises(ValueError, match="Unsafe SQLite identifier"):
        development.database.read_json_records(
            "not-reached",
            table="events",
            json_column="event_json",
            order_by=("sequence DESC",),
        )
