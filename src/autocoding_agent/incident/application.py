"""Platform-neutral facade for incident investigation."""

from __future__ import annotations

from pathlib import Path

from autocoding_agent.adapters.claude_code import ClaudeCodeRuntime
from autocoding_agent.adapters.json_incident_store import JsonIncidentStore
from autocoding_agent.adapters.sqlite_database import SQLiteDatabaseReader
from autocoding_agent.config import Settings, get_settings
from autocoding_agent.incident.capability_store import IncidentCapabilityStore
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import IncidentOutcome, IncidentSession
from autocoding_agent.observability import configure_file_logging
from autocoding_agent.ports.database import DatabaseReader
from autocoding_agent.ports.structured_runtime import StructuredRuntime


class IncidentApplication:
    """Stable entry point for CLI, a future desktop view, and DingTalk."""

    def __init__(self, engine: IncidentEngine, log_path: Path | None = None) -> None:
        self._engine = engine
        self.log_path = log_path

    def start(
        self,
        workspace: str | Path,
        problem: str,
        page_hint: str | None = None,
        *,
        source: str = "manual",
        external_reference: str | None = None,
    ) -> IncidentOutcome:
        return self._engine.start(
            workspace,
            problem,
            page_hint,
            source=source,
            external_reference=external_reference,
        )

    def send(self, session_id: str, message: str) -> IncidentOutcome:
        return self._engine.send(session_id, message)

    def outcome(self, session_id: str) -> IncidentOutcome:
        return self._engine.outcome(session_id)

    def get_session(self, session_id: str) -> IncidentSession:
        return self._engine.get_session(session_id)

    def list_sessions(self) -> list[IncidentSession]:
        return self._engine.list_sessions()


def build_incident_application(
    settings: Settings | None = None,
    runtime: StructuredRuntime | None = None,
    database: DatabaseReader | None = None,
    sqlite_path: str | Path | None = None,
    database_reference: str | None = None,
) -> IncidentApplication:
    configured = settings or get_settings()
    configured.data_dir.mkdir(parents=True, exist_ok=True)
    log_path = configure_file_logging(configured.data_dir)
    selected_path = sqlite_path or configured.incident_sqlite_path
    selected_reference = database_reference or (
        str(Path(selected_path).expanduser().resolve()) if selected_path is not None else None
    )
    selected_database = database
    if selected_database is None and selected_path is not None:
        selected_database = SQLiteDatabaseReader(
            selected_path,
            max_rows=configured.database_max_rows,
            query_timeout_seconds=configured.database_query_timeout_seconds,
        )
    engine = IncidentEngine(
        runtime=runtime or ClaudeCodeRuntime(configured),
        sessions=JsonIncidentStore(configured.data_dir),
        database=selected_database,
        max_query_rounds=configured.database_max_query_rounds,
        database_reference=selected_reference,
        capabilities=IncidentCapabilityStore(configured.data_dir),
        model=configured.claude_model,
    )
    return IncidentApplication(engine, log_path)
