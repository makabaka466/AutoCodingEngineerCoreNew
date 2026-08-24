"""Platform-independent application facade used by CLI, UI, and future adapters."""

from __future__ import annotations

from pathlib import Path

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.adapters.claude_code import ClaudeCodeRuntime
from autocoding_agent.adapters.json_session_store import JsonSessionStore
from autocoding_agent.config import Settings, get_settings
from autocoding_agent.core.engine import AgentEngine
from autocoding_agent.core.models import AgentOutcome, AgentSession
from autocoding_agent.core.policies import ExecutionPolicy
from autocoding_agent.observability import configure_file_logging
from autocoding_agent.ports.database import DatabaseReader
from autocoding_agent.ports.runtime import AgentRuntime
from autocoding_agent.skills import SkillRegistry
from autocoding_agent.workspace_knowledge import PROJECT_KNOWLEDGE_ROOT


class AgentApplication:
    """The stable API every delivery platform calls."""

    def __init__(self, engine: AgentEngine, log_path: Path | None = None) -> None:
        self._engine = engine
        self.log_path = log_path

    def start(self, workspace: str | Path, message: str) -> AgentOutcome:
        return self._engine.start(workspace, message)

    def send(self, session_id: str, message: str) -> AgentOutcome:
        return self._engine.send(session_id, message)

    def approve(self, session_id: str) -> AgentOutcome:
        return self._engine.approve(session_id)

    def reject(self, session_id: str, reason: str = "") -> AgentOutcome:
        return self._engine.reject(session_id, reason)

    def outcome(self, session_id: str) -> AgentOutcome:
        return self._engine.outcome(session_id)

    def get_session(self, session_id: str) -> AgentSession:
        return self._engine.get_session(session_id)

    def list_sessions(self) -> list[AgentSession]:
        return self._engine.list_sessions()


def build_application(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
    database: DatabaseReader | None = None,
    database_reference: str | None = None,
) -> AgentApplication:
    configured = settings or get_settings()
    configured.data_dir.mkdir(parents=True, exist_ok=True)
    log_path = configure_file_logging(configured.data_dir)
    engine = AgentEngine(
        runtime=runtime or ClaudeCodeRuntime(configured),
        sessions=JsonSessionStore(configured.data_dir),
        capabilities=CapabilityStore(
            configured.data_dir,
            knowledge_root=PROJECT_KNOWLEDGE_ROOT / "development",
        ),
        skills=SkillRegistry(),
        policy=ExecutionPolicy(),
        model=configured.claude_model,
        database=database,
        database_reference=database_reference,
        max_query_rounds=configured.database_max_query_rounds,
    )
    return AgentApplication(engine, log_path)
