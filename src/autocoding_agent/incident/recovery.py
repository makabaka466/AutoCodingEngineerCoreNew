"""Read-only incident recovery built on the shared orphaned-run scanner."""

from __future__ import annotations

from datetime import datetime

from autocoding_agent.core.models import AgentEvent, ChatMessage, EventType, MessageRole, utc_now
from autocoding_agent.core.recovery.models import RecoveryScanResult
from autocoding_agent.core.recovery.scanner import OrphanedRunScanner
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import AgentStateMachine
from autocoding_agent.core.state_machine.models import AgentCommand, AgentCommandType, TaskState
from autocoding_agent.incident.models import IncidentDecision, IncidentSession, IncidentStatus
from autocoding_agent.incident.ports import IncidentSessionStore


class IncidentRecoveryManager:
    """Pause abandoned incident inspections without automatically replaying queries."""

    def __init__(
        self,
        sessions: IncidentSessionStore,
        state_machine: AgentStateMachine,
    ) -> None:
        self.sessions = sessions
        self.state_machine = state_machine

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
        session: IncidentSession,
        run: RuntimeRunRecord,
        now: datetime,
    ) -> None:
        command = AgentCommand(
            task_id=session.id,
            type=AgentCommandType.RESUME_TASK,
            expected_version=session.version,
            actor="host_recovery",
            payload={"orphaned_run_id": run.id, "workflow": "incident"},
        )
        run.status = RunStatus.INTERRUPTED
        run.completed_at = now
        run.heartbeat_at = max(run.heartbeat_at, now)
        run.terminal_reason = "The incident Runtime owner ended before recording a result."
        message = (
            "异常诊断的只读 Runtime 在返回结果前中断。任务已暂停，没有自动重放代码读取或"
            "数据库查询；请选择继续只读调查、重新调查或取消。"
        )
        decision = IncidentDecision(status=IncidentStatus.FAILED, message=message)
        session.status = decision.status
        session.last_decision = decision
        session.messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=message))
        self.state_machine.transition(
            session,
            TaskState.PAUSED,
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
                    "workflow": "incident",
                    "interrupted_mode": run.mode,
                    "target_state": TaskState.PAUSED.value,
                },
            )
        )
        session.updated_at = utc_now()
        self.sessions.save(session)
