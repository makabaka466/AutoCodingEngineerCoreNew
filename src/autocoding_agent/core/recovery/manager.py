"""Conservative startup reconciliation for orphaned Runtime runs."""

from __future__ import annotations

from datetime import datetime

from autocoding_agent.core.artifacts.recorder import ArtifactRecorder
from autocoding_agent.core.audit.recorder import DecisionRecorder
from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentSession,
    AgentStatus,
    ChatMessage,
    EventType,
    MessageRole,
    utc_now,
)
from autocoding_agent.core.recovery.models import RecoveryScanResult
from autocoding_agent.core.recovery.scanner import OrphanedRunScanner
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import AgentStateMachine
from autocoding_agent.core.state_machine.models import AgentCommand, AgentCommandType, TaskState
from autocoding_agent.ports.session_store import SessionStore


class RecoveryManager:
    """Mark dead in-flight runs without ever replaying their side effects."""

    def __init__(
        self,
        sessions: SessionStore,
        state_machine: AgentStateMachine,
        artifacts: ArtifactRecorder | None = None,
        decisions: DecisionRecorder | None = None,
    ) -> None:
        self.sessions = sessions
        self.state_machine = state_machine
        self.artifacts = artifacts
        self.decisions = decisions or DecisionRecorder()

    def reconcile(
        self,
        *,
        current_owner_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> RecoveryScanResult:
        scanner = OrphanedRunScanner(
            self.sessions,  # type: ignore[arg-type]
            is_terminal=self.state_machine.is_terminal,
            recover=lambda session, run, observed_at: self._recover(
                session,  # type: ignore[arg-type]
                run,
                observed_at,
            ),
        )
        return scanner.reconcile(
            current_owner_id=current_owner_id,
            lease_seconds=lease_seconds,
            now=now,
        )

    def _recover(
        self,
        session: AgentSession,
        run: RuntimeRunRecord,
        now: datetime,
    ) -> None:
        command = AgentCommand(
            task_id=session.id,
            type=AgentCommandType.RESUME_TASK,
            expected_version=session.version,
            actor="host_recovery",
            payload={"orphaned_run_id": run.id},
        )
        if run.status == RunStatus.STARTED:
            run.status = RunStatus.INTERRUPTED
            run.completed_at = now
            run.heartbeat_at = max(run.heartbeat_at, now)
            run.terminal_reason = "The owning process ended before recording a terminal run result."
        target = TaskState.PAUSED if run.mode == "inspect" else TaskState.RECOVERY_REQUIRED
        message = (
            "A read-only Runtime run was interrupted. The task is paused and can be inspected "
            "again safely."
            if target == TaskState.PAUSED
            else "A side-effect-capable Runtime run ended without a terminal result. Review the "
            "recovery report before choosing read-only inspection, replanning, or cancellation."
        )
        decision = AgentDecision(
            status=AgentStatus.FAILED,
            message=message,
            reason=run.terminal_reason,
        )
        session.status = decision.status
        session.last_decision = decision
        session.pending_approval = None
        session.messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=message))
        self.decisions.record(
            session,
            decision,
            model="host-recovery",
            runtime_session_id=run.runtime_session_id,
            command_id=command.id,
            actor="host_recovery",
        )
        if self.artifacts is not None:
            try:
                self.artifacts.record_recovery_report(session, run, command.id)
            except Exception as exc:
                session.events.append(
                    AgentEvent(
                        type=EventType.ARTIFACT_FAILED,
                        message=f"Recovery report storage failed: {exc}",
                        actor="host_recovery",
                        command_id=command.id,
                    )
                )
        self.state_machine.transition(
            session,
            target,
            reason=message,
            actor="host_recovery",
            command_id=command.id,
            expected_version=command.expected_version,
        )
        session.events.append(
            AgentEvent(
                type=EventType.RECOVERY_REQUIRED,
                message=message,
                actor="host_recovery",
                command_id=command.id,
                correlation_id=run.id,
                data={
                    "run_id": run.id,
                    "interrupted_mode": run.mode,
                    "target_state": target.value,
                },
            )
        )
        session.updated_at = utc_now()
        self.sessions.save(session)
