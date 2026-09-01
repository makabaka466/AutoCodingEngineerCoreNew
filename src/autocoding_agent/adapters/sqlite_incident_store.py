"""Transactional incident snapshots with append-only shared lifecycle events."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from autocoding_agent.adapters.sqlite_runtime import (
    INCIDENT_LAYOUT,
    SQLiteRuntimeDatabase,
)
from autocoding_agent.adapters.sqlite_task_store import (
    ConcurrentSessionUpdate,
    EventStoreCorruption,
)
from autocoding_agent.core.models import AgentEvent, EventType
from autocoding_agent.core.runtime.models import RuntimeRunRecord
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.models import IncidentSession

logger = logging.getLogger("autocoding_agent.store.sqlite_incident")


class SQLiteIncidentStore:
    """Persist incident snapshots around the shared SQLite lifecycle mechanics.

    The incident schema and migration stay local to this store; connection policy and
    immutable Event/Run/Command records are delegated to ``SQLiteRuntimeDatabase``.
    """

    def __init__(self, root: str | Path, *, migrate_legacy_json: bool = True) -> None:
        self.database = SQLiteRuntimeDatabase(root, INCIDENT_LAYOUT)
        self.data_dir = self.database.data_dir
        self.runtime_dir = self.database.runtime_dir
        self.path = self.database.path
        self._initialize()
        if migrate_legacy_json:
            self._migrate_legacy_sessions()

    def create(self, session: IncidentSession) -> None:
        task_id = self._safe_id(session.id)
        original_revision, original_sequences = self._capture_mutable_fields(session)
        try:
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._task_exists(connection, task_id):
                    raise FileExistsError(f"Incident session already exists: {task_id}")
                self.database.append_events(connection, session, EventStoreCorruption)
                self.database.upsert_runs(connection, session, EventStoreCorruption)
                self.database.append_command_receipts(
                    connection, session, EventStoreCorruption
                )
                session.revision = 1
                connection.execute(
                    """
                    INSERT INTO incident_tasks(
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
            self._restore_mutable_fields(session, original_revision, original_sequences)
            raise

    def load(self, session_id: str) -> IncidentSession:
        task_id = self._safe_id(session_id)
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM incident_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown incident session: {task_id}")
        return IncidentSession.model_validate_json(row[0])

    def save(self, session: IncidentSession) -> None:
        task_id = self._safe_id(session.id)
        original_revision, original_sequences = self._capture_mutable_fields(session)
        try:
            with closing(self.database.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT revision FROM incident_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown incident session: {task_id}")
                stored_revision = int(row[0])
                if stored_revision != session.revision:
                    raise ConcurrentSessionUpdate(
                        f"Incident {task_id} changed: expected revision {session.revision}, "
                        f"stored {stored_revision}."
                    )
                self.database.append_events(connection, session, EventStoreCorruption)
                self.database.upsert_runs(connection, session, EventStoreCorruption)
                self.database.append_command_receipts(
                    connection, session, EventStoreCorruption
                )
                session.revision = stored_revision + 1
                cursor = connection.execute(
                    """
                    UPDATE incident_tasks
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
                    raise ConcurrentSessionUpdate(
                        f"Incident {task_id} changed during the update."
                    )
                connection.commit()
        except Exception:
            self._restore_mutable_fields(session, original_revision, original_sequences)
            raise

    def list(self) -> list[IncidentSession]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json
                FROM incident_tasks
                ORDER BY updated_at DESC, task_id DESC
                """
            ).fetchall()
        return [IncidentSession.model_validate_json(row[0]) for row in rows]

    def list_events(self, task_id: str) -> list[AgentEvent]:
        payloads = self.database.read_json_records(
            task_id,
            table=INCIDENT_LAYOUT.events,
            json_column="event_json",
            order_by=("sequence",),
        )
        return [AgentEvent.model_validate_json(payload) for payload in payloads]

    def list_runs(self, task_id: str) -> list[RuntimeRunRecord]:
        payloads = self.database.read_json_records(
            task_id,
            table=INCIDENT_LAYOUT.runs,
            json_column="run_json",
            order_by=("started_at", "run_id"),
        )
        return [RuntimeRunRecord.model_validate_json(payload) for payload in payloads]

    def replay_task_state(self, task_id: str) -> TaskState:
        return self.database.replay_state(
            self.list_events(task_id),
            EventStoreCorruption,
            label="incident",
        )

    def _initialize(self) -> None:
        self.database.initialize(
            """
                CREATE TABLE IF NOT EXISTS incident_tasks (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incident_events (
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, sequence),
                    FOREIGN KEY(task_id) REFERENCES incident_tasks(task_id)
                        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
                );

                CREATE INDEX IF NOT EXISTS idx_incident_events_type
                ON incident_events(task_id, event_type, sequence);

                CREATE TABLE IF NOT EXISTS incident_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    run_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES incident_tasks(task_id)
                        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
                );

                CREATE INDEX IF NOT EXISTS idx_incident_runs_status
                ON incident_runs(task_id, status, heartbeat_at);

                CREATE TABLE IF NOT EXISTS incident_commands (
                    command_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES incident_tasks(task_id)
                        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
                );
            """
        )

    def _migrate_legacy_sessions(self) -> None:
        legacy_dir = self.data_dir / "incidents"
        if not legacy_dir.is_dir():
            return
        for path in sorted(legacy_dir.glob("*.json")):
            try:
                session = IncidentSession.model_validate_json(path.read_text(encoding="utf-8"))
                with closing(self.database.connect()) as connection:
                    if self._task_exists(connection, session.id):
                        continue
                self._add_legacy_import_events(session, path)
                self.create(session)
                logger.info(
                    "legacy_incident_imported session_id=%s source=%s",
                    session.id,
                    path,
                )
            except Exception as exc:
                logger.error(
                    "legacy_incident_import_failed source=%s reason=%s",
                    path,
                    " ".join(str(exc).split())[:500],
                )

    @staticmethod
    def _add_legacy_import_events(session: IncidentSession, source: Path) -> None:
        if not any(event.type == EventType.TASK_CREATED for event in session.events):
            session.events.insert(
                0,
                AgentEvent(
                    type=EventType.TASK_CREATED,
                    message="Imported legacy incident task.",
                    actor="migration",
                    data={"state": TaskState.CREATED.value, "source": source.name},
                ),
            )
        if session.task_state != TaskState.CREATED and not any(
            event.type == EventType.STATE_TRANSITIONED for event in session.events
        ):
            session.events.insert(
                1,
                AgentEvent(
                    type=EventType.STATE_TRANSITIONED,
                    message="Inferred incident lifecycle while importing legacy JSON.",
                    actor="migration",
                    data={
                        "from": TaskState.CREATED.value,
                        "to": session.task_state.value,
                        "reason": "Legacy incident did not persist lifecycle transitions.",
                        "version": session.version,
                    },
                ),
            )

    @staticmethod
    def _safe_id(task_id: str) -> str:
        return SQLiteRuntimeDatabase.safe_id(task_id)

    @staticmethod
    def _snapshot_json(session: IncidentSession) -> str:
        return SQLiteRuntimeDatabase.snapshot_json(session)

    def _task_exists(self, connection: sqlite3.Connection, task_id: str) -> bool:
        return self.database.task_exists(connection, task_id)

    @staticmethod
    def _capture_mutable_fields(
        session: IncidentSession,
    ) -> tuple[int, dict[str, int | None]]:
        return SQLiteRuntimeDatabase.capture_mutable_fields(session)

    @staticmethod
    def _restore_mutable_fields(
        session: IncidentSession,
        revision: int,
        sequences: dict[str, int | None],
    ) -> None:
        SQLiteRuntimeDatabase.restore_mutable_fields(session, revision, sequences)
