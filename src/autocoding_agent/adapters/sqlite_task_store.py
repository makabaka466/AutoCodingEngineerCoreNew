"""Transactional task snapshots and append-only lifecycle events."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from autocoding_agent.core.artifacts.models import ArtifactRecord
from autocoding_agent.core.audit.models import DecisionRecord
from autocoding_agent.core.models import AgentEvent, AgentSession, EventType
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.models import CommandReceipt, TaskState

logger = logging.getLogger("autocoding_agent.store.sqlite_task")


class ConcurrentSessionUpdate(RuntimeError):
    """The stored snapshot changed after this caller loaded it."""


class EventStoreCorruption(RuntimeError):
    """Persisted events cannot be replayed as one valid lifecycle timeline."""


class SQLiteTaskStore:
    """Store a task snapshot and its new events in the same SQLite transaction."""

    def __init__(self, root: str | Path, *, migrate_legacy_json: bool = True) -> None:
        self.data_dir = Path(root).expanduser().resolve()
        self.runtime_dir = self.data_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime_dir / "agent-runtime.db"
        self._initialize()
        if migrate_legacy_json:
            self._migrate_legacy_sessions()

    def create(self, session: AgentSession) -> None:
        task_id = self._safe_id(session.id)
        original_revision, original_sequences = self._capture_mutable_store_fields(session)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._task_exists(connection, task_id):
                    raise FileExistsError(f"Session already exists: {task_id}")
                self._append_new_events(connection, session)
                self._append_new_decisions(connection, session)
                self._append_new_artifacts(connection, session)
                self._upsert_runs(connection, session)
                self._append_command_receipts(connection, session)
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
        with closing(self._connect()) as connection:
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
            with closing(self._connect()) as connection:
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

                self._append_new_events(connection, session)
                self._append_new_decisions(connection, session)
                self._append_new_artifacts(connection, session)
                self._upsert_runs(connection, session)
                self._append_command_receipts(connection, session)
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
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT snapshot_json FROM tasks ORDER BY updated_at DESC, task_id DESC"
            ).fetchall()
        return [AgentSession.model_validate_json(row[0]) for row in rows]

    def list_events(self, task_id: str) -> list[AgentEvent]:
        safe_id = self._safe_id(task_id)
        with closing(self._connect()) as connection:
            if not self._task_exists(connection, safe_id):
                raise KeyError(f"Unknown session: {safe_id}")
            rows = connection.execute(
                """
                SELECT event_json
                FROM events
                WHERE task_id = ?
                ORDER BY sequence
                """,
                (safe_id,),
            ).fetchall()
        return [AgentEvent.model_validate_json(row[0]) for row in rows]

    def list_decisions(self, task_id: str) -> list[DecisionRecord]:
        safe_id = self._safe_id(task_id)
        with closing(self._connect()) as connection:
            if not self._task_exists(connection, safe_id):
                raise KeyError(f"Unknown session: {safe_id}")
            rows = connection.execute(
                """
                SELECT decision_json
                FROM decisions
                WHERE task_id = ?
                ORDER BY created_at, decision_id
                """,
                (safe_id,),
            ).fetchall()
        return [DecisionRecord.model_validate_json(row[0]) for row in rows]

    def list_artifacts(self, task_id: str) -> list[ArtifactRecord]:
        safe_id = self._safe_id(task_id)
        with closing(self._connect()) as connection:
            if not self._task_exists(connection, safe_id):
                raise KeyError(f"Unknown session: {safe_id}")
            rows = connection.execute(
                """
                SELECT artifact_json
                FROM artifacts
                WHERE task_id = ?
                ORDER BY created_at, artifact_id
                """,
                (safe_id,),
            ).fetchall()
        return [ArtifactRecord.model_validate_json(row[0]) for row in rows]

    def list_runs(self, task_id: str) -> list[RuntimeRunRecord]:
        safe_id = self._safe_id(task_id)
        with closing(self._connect()) as connection:
            if not self._task_exists(connection, safe_id):
                raise KeyError(f"Unknown session: {safe_id}")
            rows = connection.execute(
                """
                SELECT run_json
                FROM runs
                WHERE task_id = ?
                ORDER BY started_at, run_id
                """,
                (safe_id,),
            ).fetchall()
        return [RuntimeRunRecord.model_validate_json(row[0]) for row in rows]

    def list_command_receipts(self, task_id: str) -> list[CommandReceipt]:
        safe_id = self._safe_id(task_id)
        with closing(self._connect()) as connection:
            if not self._task_exists(connection, safe_id):
                raise KeyError(f"Unknown session: {safe_id}")
            rows = connection.execute(
                """
                SELECT receipt_json
                FROM commands
                WHERE task_id = ?
                ORDER BY created_at, command_id
                """,
                (safe_id,),
            ).fetchall()
        return [CommandReceipt.model_validate_json(row[0]) for row in rows]

    def replay_task_state(self, task_id: str) -> TaskState:
        """Rebuild lifecycle state from ordered transition facts and verify their chain."""

        current = TaskState.CREATED
        for event in self.list_events(task_id):
            if event.type != EventType.STATE_TRANSITIONED:
                continue
            try:
                source = TaskState(str(event.data["from"]))
                target = TaskState(str(event.data["to"]))
            except (KeyError, ValueError) as exc:
                raise EventStoreCorruption(
                    f"State event {event.id} has an invalid transition payload."
                ) from exc
            if source != current:
                raise EventStoreCorruption(
                    f"State event {event.id} expected {source.value}, replay is "
                    f"currently {current.value}."
                )
            current = target
        return current

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
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
        session: AgentSession,
    ) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE task_id = ?",
            (session.id,),
        ).fetchone()
        next_sequence = int(row[0]) + 1
        for event in session.events:
            existing = connection.execute(
                "SELECT task_id, sequence, event_json FROM events WHERE event_id = ?",
                (event.id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise EventStoreCorruption(
                        f"Event id {event.id} already belongs to another task."
                    )
                stored_event = AgentEvent.model_validate_json(existing[2])
                candidate = event.model_copy(update={"sequence": int(existing[1])})
                if candidate != stored_event:
                    raise EventStoreCorruption(
                        f"Persisted event {event.id} was modified after append."
                    )
                event.sequence = int(existing[1])
                continue

            if event.sequence is not None:
                raise EventStoreCorruption(
                    f"Unpersisted event {event.id} already has sequence {event.sequence}."
                )
            event.sequence = next_sequence
            event_json = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO events(
                    task_id, sequence, event_id, schema_version, event_type,
                    timestamp, actor, correlation_id, causation_id, command_id, event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    next_sequence,
                    event.id,
                    event.schema_version,
                    event.type.value,
                    event.created_at.isoformat(),
                    event.actor,
                    event.correlation_id,
                    event.causation_id,
                    event.command_id,
                    event_json,
                ),
            )
            next_sequence += 1

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

    def _upsert_runs(
        self,
        connection: sqlite3.Connection,
        session: AgentSession,
    ) -> None:
        for run in session.runs:
            existing = connection.execute(
                "SELECT task_id, run_json FROM runs WHERE run_id = ?",
                (run.id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, task_id, state, mode, status, started_at,
                        heartbeat_at, completed_at, run_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        session.id,
                        run.state.value,
                        run.mode,
                        run.status.value,
                        run.started_at.isoformat(),
                        run.heartbeat_at.isoformat(),
                        run.completed_at.isoformat() if run.completed_at else None,
                        json.dumps(run.model_dump(mode="json"), ensure_ascii=False),
                    ),
                )
                continue
            if existing[0] != session.id:
                raise EventStoreCorruption(f"Run id {run.id} already belongs to another task.")
            stored = RuntimeRunRecord.model_validate_json(existing[1])
            if stored.status != RunStatus.STARTED and stored != run:
                raise EventStoreCorruption(
                    f"Persisted terminal run {run.id} was modified after completion."
                )
            if stored.status == RunStatus.STARTED:
                immutable_fields = (
                    "task_id",
                    "state",
                    "mode",
                    "owner_id",
                    "owner_pid",
                    "started_at",
                )
                if any(getattr(stored, field) != getattr(run, field) for field in immutable_fields):
                    raise EventStoreCorruption(
                        f"Persisted run {run.id} changed immutable identity fields."
                    )
                if run.heartbeat_at < stored.heartbeat_at:
                    raise EventStoreCorruption(f"Run {run.id} heartbeat moved backwards.")
                connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, heartbeat_at = ?, completed_at = ?, run_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        run.status.value,
                        run.heartbeat_at.isoformat(),
                        run.completed_at.isoformat() if run.completed_at else None,
                        json.dumps(run.model_dump(mode="json"), ensure_ascii=False),
                        run.id,
                    ),
                )

    def _append_command_receipts(
        self,
        connection: sqlite3.Connection,
        session: AgentSession,
    ) -> None:
        for receipt in session.command_receipts:
            existing = connection.execute(
                "SELECT task_id, receipt_json FROM commands WHERE command_id = ?",
                (receipt.command_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise EventStoreCorruption(
                        f"Command id {receipt.command_id} already belongs to another task."
                    )
                if CommandReceipt.model_validate_json(existing[1]) != receipt:
                    raise EventStoreCorruption(
                        f"Persisted command receipt {receipt.command_id} was modified."
                    )
                continue
            connection.execute(
                """
                INSERT INTO commands(
                    command_id, task_id, command_type, completed_version,
                    created_at, receipt_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.command_id,
                    session.id,
                    receipt.command_type.value,
                    receipt.completed_version,
                    receipt.created_at.isoformat(),
                    json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False),
                ),
            )

    def _migrate_legacy_sessions(self) -> None:
        legacy_dir = self.data_dir / "sessions"
        if not legacy_dir.is_dir():
            return
        for path in sorted(legacy_dir.glob("*.json")):
            try:
                session = AgentSession.model_validate_json(path.read_text(encoding="utf-8"))
                with closing(self._connect()) as connection:
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

    @staticmethod
    def _task_exists(connection: sqlite3.Connection, task_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _safe_id(task_id: str) -> str:
        return str(UUID(task_id))

    @staticmethod
    def _snapshot_json(session: AgentSession) -> str:
        return json.dumps(session.model_dump(mode="json"), ensure_ascii=False, indent=2)

    @staticmethod
    def _capture_mutable_store_fields(
        session: AgentSession,
    ) -> tuple[int, dict[str, int | None]]:
        return session.revision, {event.id: event.sequence for event in session.events}

    @staticmethod
    def _restore_mutable_store_fields(
        session: AgentSession,
        revision: int,
        sequences: dict[str, int | None],
    ) -> None:
        session.revision = revision
        for event in session.events:
            event.sequence = sequences.get(event.id)
