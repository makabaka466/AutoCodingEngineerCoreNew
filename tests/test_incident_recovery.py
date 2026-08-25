from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from autocoding_agent.adapters.sqlite_incident_store import SQLiteIncidentStore
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentEvent, AgentUsage, EventType, RuntimeTurn
from autocoding_agent.core.recovery.models import RecoveryAction
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import AgentStateMachine
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import (
    IncidentDecision,
    IncidentSession,
    IncidentStatus,
    LocatedPage,
)
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class CompleteAfterResumeRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[IncidentDecision],
    ) -> StructuredRuntimeResult[IncidentDecision]:
        self.calls += 1
        return StructuredRuntimeResult(
            output=IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Recovered diagnosis complete.",
                page=LocatedPage(
                    name="Orders",
                    source_paths=["src/orders.py"],
                    explanation="The page was rechecked after recovery.",
                ),
                diagnosis="The task resumed from current read-only evidence.",
            ),
            runtime_session_id=turn.session_id,
            usage=AgentUsage(turns=1),
        )


def test_orphaned_incident_run_pauses_and_resumes_without_automatic_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    session = IncidentSession(workspace=str(workspace), problem="Order page is stale")
    session.events.append(
        AgentEvent(type=EventType.TASK_CREATED, message="Created incident.", actor="user")
    )
    machine = AgentStateMachine()
    machine.transition(session, TaskState.INSPECTING, reason="Started incident inspection.")
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    session.runs.append(
        RuntimeRunRecord(
            task_id=session.id,
            state=TaskState.INSPECTING,
            mode="inspect",
            owner_id="dead-owner",
            owner_pid=99_999_999,
            started_at=old,
            heartbeat_at=old,
            runtime_session_id="incident-before-crash",
        )
    )
    SQLiteIncidentStore(data_dir, migrate_legacy_json=False).create(session)
    runtime = CompleteAfterResumeRuntime()
    settings = Settings(
        claude_command="claude-test.exe",
        claude_model="test-model",
        claude_timeout_seconds=30,
        data_dir=data_dir,
        runtime_lease_seconds=5,
    )

    application = build_incident_application(settings=settings, runtime=runtime)
    paused = application.get_session(session.id)

    assert application.recovery_scan.recovered_task_ids == [session.id]
    assert paused.task_state == TaskState.PAUSED
    assert paused.runs[0].status == RunStatus.INTERRUPTED
    assert runtime.calls == 0

    completed = application.resume(session.id, RecoveryAction.READ_ONLY_INSPECT)

    assert completed.task_state == TaskState.COMPLETED
    assert runtime.calls == 1
    assert SQLiteIncidentStore(data_dir).replay_task_state(session.id) == TaskState.COMPLETED
