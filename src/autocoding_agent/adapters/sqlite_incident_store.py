"""Transactional incident snapshots with append-only shared lifecycle events."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from autocoding_agent.adapters.sqlite_task_store import (
    ConcurrentSessionUpdate,
    EventStoreCorruption,
)
from autocoding_agent.core.models import AgentEvent, EventType
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.models import CommandReceipt, TaskState
from autocoding_agent.incident.models import IncidentSession

logger = logging.getLogger("autocoding_agent.store.sqlite_incident")


class SQLiteIncidentStore:
    """Persist incident aggregate and lifecycle facts in the shared runtime database."""

    def __init__(self, root: str | Path, *, migrate_legacy_json: bool = True) -> None:
        self.data_dir = Path(root).expanduser().resolve()
        self.runtime_dir = self.data_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime_dir / "agent-runtime.db"
        self._initialize()
        if migrate_legacy_json:
            self._migrate_legacy_sessions()

    def create(self, session: IncidentSession) -> None:
        task_id = self._safe_id(session.id)
        original_revision, original_sequences = self._capture_mutable_fields(session)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._task_exists(connection, task_id):
                    raise FileExistsError(f"Incident session already exists: {task_id}")
                self._append_new_events(connection, session)
                self._upsert_runs(connection, session)
                self._append_command_receipts(connection, session)
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
        with closing(self._connect()) as connection:
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
            with closing(self._connect()) as connection:
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
                self._append_new_events(connection, session)
                self._upsert_runs(connection, session)
                self._append_command_receipts(connection, session)
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
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT snapshot_json
                FROM incident_tasks
                ORDER BY updated_at DESC, task_id DESC
                """
            ).fetchall()
        return [IncidentSession.model_validate_json(row[0]) for row in rows]

    def list_events(self, task_id: str) -> list[AgentEvent]:
        safe_id = self._safe_id(task_id)
        with closing(self._connect()) as connection:
            if not self._task_exists(connection, safe_id):
                raise KeyError(f"Unknown incident session: {safe_id}")
            rows = connection.execute(
                """
                SELECT event_json
                FROM incident_events
                WHERE task_id = ?
                ORDER BY sequence
                """,
                (safe_id,),
            ).fetchall()
        return [AgentEvent.model_validate_json(row[0]) for row in rows]

    def list_runs(self, task_id: str) -> list[RuntimeRunRecord]:
        safe_id = self._safe_id(task_id)
        with closing(self._connect()) as connection:
            if not self._task_exists(connection, safe_id):
                raise KeyError(f"Unknown incident session: {safe_id}")
            rows = connection.execute(
                """
                SELECT run_json
                FROM incident_runs
                WHERE task_id = ?
                ORDER BY started_at, run_id
                """,
                (safe_id,),
            ).fetchall()
        return [RuntimeRunRecord.model_validate_json(row[0]) for row in rows]

    def replay_task_state(self, task_id: str) -> TaskState:
        current = TaskState.CREATED
        for event in self.list_events(task_id):
            if event.type != EventType.STATE_TRANSITIONED:
                continue
            try:
                source = TaskState(str(event.data["from"]))
                target = TaskState(str(event.data["to"]))
            except (KeyError, ValueError) as exc:
                raise EventStoreCorruption(
                    f"Incident state event {event.id} has an invalid transition payload."
                ) from exc
            if source != current:
                raise EventStoreCorruption(
                    f"Incident state event {event.id} expected {source.value}, replay is "
                    f"currently {current.value}."
                )
            current = target
        return current

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
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
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _append_new_events(
        self,
        connection: sqlite3.Connection,
        session: IncidentSession,
    ) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM incident_events WHERE task_id = ?",
            (session.id,),
        ).fetchone()
        next_sequence = int(row[0]) + 1
        for event in session.events:
            existing = connection.execute(
                "SELECT task_id, sequence, event_json FROM incident_events WHERE event_id = ?",
                (event.id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise EventStoreCorruption(
                        f"Incident event id {event.id} belongs to another task."
                    )
                stored = AgentEvent.model_validate_json(existing[2])
                candidate = event.model_copy(update={"sequence": int(existing[1])})
                if candidate != stored:
                    raise EventStoreCorruption(
                        f"Persisted incident event {event.id} was modified after append."
                    )
                event.sequence = int(existing[1])
                continue
            if event.sequence is not None:
                raise EventStoreCorruption(
                    f"Unpersisted incident event {event.id} has a sequence."
                )
            event.sequence = next_sequence
            event_json = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO incident_events(
                    task_id, sequence, event_id, event_type, timestamp, event_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    next_sequence,
                    event.id,
                    event.type.value,
                    event.created_at.isoformat(),
                    event_json,
                ),
            )
            next_sequence += 1

    def _upsert_runs(
        self,
        connection: sqlite3.Connection,
        session: IncidentSession,
    ) -> None:
        for run in session.runs:
            existing = connection.execute(
                "SELECT task_id, run_json FROM incident_runs WHERE run_id = ?",
                (run.id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO incident_runs(
                        run_id, task_id, status, started_at, heartbeat_at, run_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        session.id,
                        run.status.value,
                        run.started_at.isoformat(),
                        run.heartbeat_at.isoformat(),
                        json.dumps(run.model_dump(mode="json"), ensure_ascii=False),
                    ),
                )
                continue
            if existing[0] != session.id:
                raise EventStoreCorruption(f"Incident run {run.id} belongs to another task.")
            stored = RuntimeRunRecord.model_validate_json(existing[1])
            if stored.status != RunStatus.STARTED and stored != run:
                raise EventStoreCorruption(
                    f"Persisted terminal incident run {run.id} was modified."
                )
            if stored.status == RunStatus.STARTED:
                connection.execute(
                    """
                    UPDATE incident_runs
                    SET status = ?, heartbeat_at = ?, run_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        run.status.value,
                        run.heartbeat_at.isoformat(),
                        json.dumps(run.model_dump(mode="json"), ensure_ascii=False),
                        run.id,
                    ),
                )

    def _append_command_receipts(
        self,
        connection: sqlite3.Connection,
        session: IncidentSession,
    ) -> None:
        for receipt in session.command_receipts:
            existing = connection.execute(
                "SELECT task_id, receipt_json FROM incident_commands WHERE command_id = ?",
                (receipt.command_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise EventStoreCorruption(
                        f"Incident command {receipt.command_id} belongs to another task."
                    )
                if CommandReceipt.model_validate_json(existing[1]) != receipt:
                    raise EventStoreCorruption(
                        f"Persisted incident command {receipt.command_id} was modified."
                    )
                continue
            connection.execute(
                """
                INSERT INTO incident_commands(command_id, task_id, created_at, receipt_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    receipt.command_id,
                    session.id,
                    receipt.created_at.isoformat(),
                    json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False),
                ),
            )

    def _migrate_legacy_sessions(self) -> None:
        legacy_dir = self.data_dir / "incidents"
        if not legacy_dir.is_dir():
            return
        for path in sorted(legacy_dir.glob("*.json")):
            try:
                session = IncidentSession.model_validate_json(path.read_text(encoding="utf-8"))
                with closing(self._connect()) as connection:
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
        return str(UUID(task_id))

    @staticmethod
    def _snapshot_json(session: IncidentSession) -> str:
        return json.dumps(session.model_dump(mode="json"), ensure_ascii=False, indent=2)

    @staticmethod
    def _task_exists(connection: sqlite3.Connection, task_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM incident_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _capture_mutable_fields(
        session: IncidentSession,
    ) -> tuple[int, dict[str, int | None]]:
        return session.revision, {event.id: event.sequence for event in session.events}

    @staticmethod
    def _restore_mutable_fields(
        session: IncidentSession,
        revision: int,
        sequences: dict[str, int | None],
    ) -> None:
        session.revision = revision
        for event in session.events:
            event.sequence = sequences.get(event.id)
