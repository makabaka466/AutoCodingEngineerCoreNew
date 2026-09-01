"""Transactional task snapshots and append-only lifecycle events."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from autocoding_agent.adapters.sqlite_runtime import (
    DEVELOPMENT_LAYOUT,
    SQLiteRuntimeDatabase,
)
from autocoding_agent.core.artifacts.models import ArtifactRecord
from autocoding_agent.core.audit.models import DecisionRecord
from autocoding_agent.core.models import AgentEvent, AgentSession, EventType
from autocoding_agent.core.runtime.models import RuntimeRunRecord
from autocoding_agent.core.state_machine.models import CommandReceipt, TaskState

logger = logging.getLogger("autocoding_agent.store.sqlite_task")


class ConcurrentSessionUpdate(RuntimeError):
    """The stored snapshot changed after this caller loaded it."""


class EventStoreCorruption(RuntimeError):
    """Persisted events cannot be replayed as one valid lifecycle timeline."""


class SQLiteTaskStore:
    """Persist development-specific snapshots around shared SQLite mechanics.

    Decisions and artifacts remain development-domain records. Connection setup plus
    Event/Run/Command append rules are delegated to ``SQLiteRuntimeDatabase``.
    """

    def __init__(self, root: str | Path, *, migrate_legacy_json: bool = True) -> None:
        self.database = SQLiteRuntimeDatabase(root, DEVELOPMENT_LAYOUT)
        self.data_dir = self.database.data_dir
        self.runtime_dir = self.database.runtime_dir
        self.path = self.database.path
        self._initialize()
        if migrate_legacy_json:
            self._migrate_legacy_sessions()

    def create(self, session: AgentSession) -> None:
        task_id = self._safe_id(session.id)
        original_revision, original_sequences = self._capture_mutable_store_fields(session)
        try:
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._task_exists(connection, task_id):
                    raise FileExistsError(f"Session already exists: {task_id}")
                self.database.append_events(connection, session, EventStoreCorruption)
                self._append_new_decisions(connection, session)
                self._append_new_artifacts(connection, session)
                self.database.upsert_runs(connection, session, EventStoreCorruption)
                self.database.append_command_receipts(
                    connection, session, EventStoreCorruption
                )
                session.revision = 1
                connection.execute(
                    """
                    INSERT INTO tasks(
                        task_id, state, state_version, revision, snapshot_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        session.task_state.value,
                        session.version,
                        session.revision,
                        self._snapshot_json(session),
                        session.created_at.isoformat(),
                        session.updated_at.isoformat(),
                    ),
                )
                connection.commit()
        except Exception:
            self._restore_mutable_store_fields(session, original_revision, original_sequences)
            raise

    def load(self, session_id: str) -> AgentSession:
        task_id = self._safe_id(session_id)
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown session: {task_id}")
        return AgentSession.model_validate_json(row[0])

    def save(self, session: AgentSession) -> None:
        task_id = self._safe_id(session.id)
        original_revision, original_sequences = self._capture_mutable_store_fields(session)
        try:
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT revision FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown session: {task_id}")
                stored_revision = int(row[0])
                if stored_revision != session.revision:
                    raise ConcurrentSessionUpdate(
                        f"Session {task_id} changed: expected revision "
                        f"{session.revision}, stored {stored_revision}."
                    )

                self.database.append_events(connection, session, EventStoreCorruption)
                self._append_new_decisions(connection, session)
                self._append_new_artifacts(connection, session)
                self.database.upsert_runs(connection, session, EventStoreCorruption)
                self.database.append_command_receipts(
                    connection, session, EventStoreCorruption
                )
                session.revision = stored_revision + 1
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET state = ?, state_version = ?, revision = ?, snapshot_json = ?,
                        updated_at = ?
                    WHERE task_id = ? AND revision = ?
                    """,
                    (
                        session.task_state.value,
                        session.version,
                        session.revision,
                        self._snapshot_json(session),
                        session.updated_at.isoformat(),
                        task_id,
                        stored_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConcurrentSessionUpdate(f"Session {task_id} changed during the update.")
                connection.commit()
        except Exception:
            self._restore_mutable_store_fields(session, original_revision, original_sequences)
            raise

    def list(self) -> list[AgentSession]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM tasks ORDER BY updated_at DESC, task_id DESC"
            ).fetchall()
        return [AgentSession.model_validate_json(row[0]) for row in rows]

    def list_events(self, task_id: str) -> list[AgentEvent]:
        payloads = self.database.read_json_records(
            task_id,
            table=DEVELOPMENT_LAYOUT.events,
            json_column="event_json",
            order_by=("sequence",),
        )
        return [AgentEvent.model_validate_json(payload) for payload in payloads]

    def list_decisions(self, task_id: str) -> list[DecisionRecord]:
        payloads = self.database.read_json_records(
            task_id,
            table="decisions",
            json_column="decision_json",
            order_by=("created_at", "decision_id"),
        )
        return [DecisionRecord.model_validate_json(payload) for payload in payloads]

    def list_artifacts(self, task_id: str) -> list[ArtifactRecord]:
        payloads = self.database.read_json_records(
            task_id,
            table="artifacts",
            json_column="artifact_json",
            order_by=("created_at", "artifact_id"),
        )
        return [ArtifactRecord.model_validate_json(payload) for payload in payloads]

    def list_runs(self, task_id: str) -> list[RuntimeRunRecord]:
        payloads = self.database.read_json_records(
            task_id,
            table=DEVELOPMENT_LAYOUT.runs,
            json_column="run_json",
            order_by=("started_at", "run_id"),
        )
        return [RuntimeRunRecord.model_validate_json(payload) for payload in payloads]

    def list_command_receipts(self, task_id: str) -> list[CommandReceipt]:
        payloads = self.database.read_json_records(
            task_id,
            table=DEVELOPMENT_LAYOUT.commands,
            json_column="receipt_json",
            order_by=("created_at", "command_id"),
        )
        return [CommandReceipt.model_validate_json(payload) for payload in payloads]

    def replay_task_state(self, task_id: str) -> TaskState:
        """Rebuild lifecycle state from ordered transition facts and verify their chain."""

        return self.database.replay_state(
            self.list_events(task_id),
            EventStoreCorruption,
            label="task",
        )

    def _initialize(self) -> None:
        self.database.initialize(
            """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    schema_version INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    correlation_id TEXT,
                    causation_id TEXT,
                    command_id TEXT,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, sequence),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                        DEFERRABLE INITIALLY DEFERRED
                );

                CREATE INDEX IF NOT EXISTS idx_events_task_type
                ON events(task_id, event_type, sequence);

                CREATE INDEX IF NOT EXISTS idx_events_command
                ON events(task_id, command_id)
                WHERE command_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    decision_type TEXT NOT NULL,
                    confidence REAL,
                    risk_level TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                        DEFERRABLE INITIALLY DEFERRED,
                    FOREIGN KEY(event_id) REFERENCES events(event_id)
                        DEFERRABLE INITIALLY DEFERRED
                );

                CREATE INDEX IF NOT EXISTS idx_decisions_task_type
                ON decisions(task_id, decision_type, created_at);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    artifact_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                        DEFERRABLE INITIALLY DEFERRED,
                    FOREIGN KEY(event_id) REFERENCES events(event_id)
                        DEFERRABLE INITIALLY DEFERRED
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_task_type
                ON artifacts(task_id, artifact_type, created_at);

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    completed_at TEXT,
                    run_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                        DEFERRABLE INITIALLY DEFERRED
                );

                CREATE INDEX IF NOT EXISTS idx_runs_task_status
                ON runs(task_id, status, heartbeat_at);

                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    completed_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                        DEFERRABLE INITIALLY DEFERRED
                );

                CREATE INDEX IF NOT EXISTS idx_commands_task_created
                ON commands(task_id, created_at);
            """
        )

    def _append_new_decisions(
        self,
        connection: sqlite3.Connection,
        session: AgentSession,
    ) -> None:
        for decision in session.decision_records:
            existing = connection.execute(
                "SELECT task_id, decision_json FROM decisions WHERE decision_id = ?",
                (decision.id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise EventStoreCorruption(
                        f"Decision id {decision.id} already belongs to another task."
                    )
                if DecisionRecord.model_validate_json(existing[1]) != decision:
                    raise EventStoreCorruption(
                        f"Persisted decision {decision.id} was modified after append."
                    )
                continue
            decision_json = json.dumps(decision.model_dump(mode="json"), ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO decisions(
                    decision_id, task_id, event_id, decision_type, confidence,
                    risk_level, created_at, decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    session.id,
                    decision.event_id,
                    decision.decision_type,
                    decision.confidence,
                    decision.risk_level.value,
                    decision.created_at.isoformat(),
                    decision_json,
                ),
            )

    def _append_new_artifacts(
        self,
        connection: sqlite3.Connection,
        session: AgentSession,
    ) -> None:
        for artifact in session.artifacts:
            existing = connection.execute(
                "SELECT task_id, artifact_json FROM artifacts WHERE artifact_id = ?",
                (artifact.id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise EventStoreCorruption(
                        f"Artifact id {artifact.id} already belongs to another task."
                    )
                if ArtifactRecord.model_validate_json(existing[1]) != artifact:
                    raise EventStoreCorruption(
                        f"Persisted artifact {artifact.id} was modified after append."
                    )
                continue
            artifact_json = json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, task_id, event_id, artifact_type, relative_path,
                    sha256, size_bytes, schema_version, created_at, artifact_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    session.id,
                    artifact.event_id,
                    artifact.type.value,
                    artifact.relative_path,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.schema_version,
                    artifact.created_at.isoformat(),
                    artifact_json,
                ),
            )

    def _migrate_legacy_sessions(self) -> None:
        legacy_dir = self.data_dir / "sessions"
        if not legacy_dir.is_dir():
            return
        for path in sorted(legacy_dir.glob("*.json")):
            try:
                session = AgentSession.model_validate_json(path.read_text(encoding="utf-8"))
                with closing(self.database.connect()) as connection:
                    exists = self._task_exists(connection, session.id)
                if exists:
                    continue
                self._add_legacy_import_events(session, path)
                self.create(session)
                logger.info(
                    "legacy_session_imported session_id=%s source=%s",
                    session.id,
                    path,
                )
            except Exception as exc:
                logger.error(
                    "legacy_session_import_failed source=%s reason=%s",
                    path,
                    " ".join(str(exc).split())[:500],
                )

    @staticmethod
    def _add_legacy_import_events(session: AgentSession, source: Path) -> None:
        synthetic: list[AgentEvent] = []
        if not any(event.type == EventType.TASK_CREATED for event in session.events):
            synthetic.append(
                AgentEvent(
                    type=EventType.TASK_CREATED,
                    message="Imported legacy JSON task.",
                    actor="migration",
                    data={"state": TaskState.CREATED.value, "source": source.name},
                )
            )
        if session.task_state != TaskState.CREATED and not any(
            event.type == EventType.STATE_TRANSITIONED for event in session.events
        ):
            synthetic.append(
                AgentEvent(
                    type=EventType.STATE_TRANSITIONED,
                    message=("Inferred lifecycle state while importing a legacy JSON task."),
                    actor="migration",
                    data={
                        "from": TaskState.CREATED.value,
                        "to": session.task_state.value,
                        "reason": "Legacy session did not persist lifecycle transitions.",
                        "version": session.version,
                    },
                )
            )
        session.events = [*synthetic, *session.events]

    def _task_exists(self, connection: sqlite3.Connection, task_id: str) -> bool:
        return self.database.task_exists(connection, task_id)

    @staticmethod
    def _safe_id(task_id: str) -> str:
        return SQLiteRuntimeDatabase.safe_id(task_id)

    @staticmethod
    def _snapshot_json(session: AgentSession) -> str:
        return SQLiteRuntimeDatabase.snapshot_json(session)

    @staticmethod
    def _capture_mutable_store_fields(
        session: AgentSession,
    ) -> tuple[int, dict[str, int | None]]:
        return SQLiteRuntimeDatabase.capture_mutable_fields(session)

    @staticmethod
    def _restore_mutable_store_fields(
        session: AgentSession,
        revision: int,
        sequences: dict[str, int | None],
    ) -> None:
        SQLiteRuntimeDatabase.restore_mutable_fields(session, revision, sequences)
