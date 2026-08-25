"""Platform-independent application facade used by CLI, UI, and future adapters."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.adapters.claude_code import ClaudeCodeRuntime
from autocoding_agent.adapters.sqlite_task_store import SQLiteTaskStore
from autocoding_agent.adapters.task_artifact_store import TaskArtifactStore
from autocoding_agent.adapters.workspace_snapshot import GitWorkspaceObserver
from autocoding_agent.config import Settings, get_settings
from autocoding_agent.core.artifacts.models import ArtifactRecord
from autocoding_agent.core.artifacts.recorder import ArtifactRecorder
from autocoding_agent.core.audit.models import ChangeExplanation
from autocoding_agent.core.engine import AgentEngine
from autocoding_agent.core.models import AgentEvent, AgentOutcome, AgentSession
from autocoding_agent.core.policies import ExecutionPolicy
from autocoding_agent.core.recovery.manager import RecoveryManager
from autocoding_agent.core.recovery.models import RecoveryAction, RecoveryScanResult
from autocoding_agent.core.runtime.models import RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import AgentStateMachine
from autocoding_agent.observability import configure_file_logging
from autocoding_agent.ports.database import DatabaseReader
from autocoding_agent.ports.runtime import AgentRuntime
from autocoding_agent.skills import SkillRegistry
from autocoding_agent.workspace_knowledge import PROJECT_KNOWLEDGE_ROOT


class AgentApplication:
    """The stable API every delivery platform calls."""

    def __init__(
        self,
        engine: AgentEngine,
        log_path: Path | None = None,
        recovery_scan: RecoveryScanResult | None = None,
    ) -> None:
        self._engine = engine
        self.log_path = log_path
        self.recovery_scan = recovery_scan or RecoveryScanResult()

    def start(
        self,
        workspace: str | Path,
        message: str,
        project: str | None = None,
    ) -> AgentOutcome:
        return self._engine.start(workspace, message, project)

    def send(
        self,
        session_id: str,
        message: str,
        command_id: str | None = None,
    ) -> AgentOutcome:
        return self._engine.send(session_id, message, command_id)

    def approve(self, session_id: str, command_id: str | None = None) -> AgentOutcome:
        return self._engine.approve(session_id, command_id)

    def reject(
        self,
        session_id: str,
        reason: str = "",
        command_id: str | None = None,
    ) -> AgentOutcome:
        return self._engine.reject(session_id, reason, command_id)

    def resume(
        self,
        session_id: str,
        action: RecoveryAction | str = RecoveryAction.READ_ONLY_INSPECT,
    ) -> AgentOutcome:
        return self._engine.resume(session_id, action)

    def pause(self, session_id: str) -> AgentOutcome:
        return self._engine.pause(session_id)

    def cancel(self, session_id: str) -> AgentOutcome:
        return self._engine.cancel(session_id)

    def outcome(self, session_id: str) -> AgentOutcome:
        return self._engine.outcome(session_id)

    def get_session(self, session_id: str) -> AgentSession:
        return self._engine.get_session(session_id)

    def list_sessions(self) -> list[AgentSession]:
        return self._engine.list_sessions()

    def explain_change(self, session_id: str, path: str) -> ChangeExplanation:
        return self._engine.explain_change(session_id, path)

    def events(self, session_id: str) -> list[AgentEvent]:
        return self._engine.get_session(session_id).events

    def artifacts(self, session_id: str) -> list[ArtifactRecord]:
        return self._engine.get_session(session_id).artifacts

    def runs(self, session_id: str) -> list[RuntimeRunRecord]:
        return self._engine.get_session(session_id).runs


def build_application(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
    database: DatabaseReader | None = None,
    database_reference: str | None = None,
) -> AgentApplication:
    configured = settings or get_settings()
    configured.data_dir.mkdir(parents=True, exist_ok=True)
    log_path = configure_file_logging(configured.data_dir)
    sessions = SQLiteTaskStore(configured.data_dir)
    state_machine = AgentStateMachine()
    artifact_recorder = ArtifactRecorder(
        TaskArtifactStore(configured.data_dir),
        GitWorkspaceObserver(),
    )
    owner_id = str(uuid4())
    recovery_scan = RecoveryManager(
        sessions,
        state_machine,
        artifact_recorder,
    ).reconcile(
        current_owner_id=owner_id,
        lease_seconds=configured.runtime_lease_seconds,
    )
    engine = AgentEngine(
        runtime=runtime or ClaudeCodeRuntime(configured),
        sessions=sessions,
        capabilities=CapabilityStore(
            configured.data_dir,
            knowledge_root=PROJECT_KNOWLEDGE_ROOT / "development",
        ),
        skills=SkillRegistry(),
        policy=ExecutionPolicy(),
        model=configured.claude_model,
        state_machine=state_machine,
        artifact_recorder=artifact_recorder,
        database=database,
        database_reference=database_reference,
        max_query_rounds=configured.database_max_query_rounds,
        max_replan_rounds=configured.agent_max_replan_rounds,
        owner_id=owner_id,
    )
    return AgentApplication(engine, log_path, recovery_scan)
