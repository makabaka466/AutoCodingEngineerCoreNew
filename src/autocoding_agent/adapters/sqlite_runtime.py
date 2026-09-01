"""Shared SQLite mechanics for development and incident task stores.

The two domains intentionally keep different snapshot tables and domain records. This
component centralizes connection policy plus the append-only Event/Run/Command rules so
their transactional guarantees cannot drift apart.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from autocoding_agent.core.models import AgentEvent, EventType
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.models import CommandReceipt, TaskState


class RuntimeStoredAggregate(Protocol):
    id: str
    revision: int
    events: list[AgentEvent]
    runs: list[RuntimeRunRecord]
    command_receipts: list[CommandReceipt]

    def model_dump(self, *, mode: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class SQLiteRuntimeLayout:
    """Trusted table layout for one domain inside the shared runtime database."""

    tasks: str
    events: str
    runs: str
    commands: str
    label: str
    detailed_events: bool
    detailed_runs: bool
    detailed_commands: bool

    def __post_init__(self) -> None:
        for name in (self.tasks, self.events, self.runs, self.commands):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"Unsafe SQLite runtime table name: {name}")


DEVELOPMENT_LAYOUT = SQLiteRuntimeLayout(
    tasks="tasks",
    events="events",
    runs="runs",
    commands="commands",
    label="task",
    detailed_events=True,
    detailed_runs=True,
    detailed_commands=True,
)

INCIDENT_LAYOUT = SQLiteRuntimeLayout(
    tasks="incident_tasks",
    events="incident_events",
    runs="incident_runs",
    commands="incident_commands",
    label="incident",
    detailed_events=False,
    detailed_runs=False,
    detailed_commands=False,
)


class SQLiteRuntimeDatabase:
    """Own shared connection and immutable lifecycle-record operations."""

    def __init__(self, root: str | Path, layout: SQLiteRuntimeLayout) -> None:
        self.data_dir = Path(root).expanduser().resolve()
        self.runtime_dir = self.data_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.runtime_dir / "agent-runtime.db"
        self.layout = layout

    def connect(self) -> sqlite3.Connection:
        """Open one consistently configured connection to the shared runtime database."""

        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self, schema: str) -> None:
        """Create one domain's tables without changing an existing schema."""

        with self.connect() as connection:
            connection.executescript(schema)
            connection.commit()

    @staticmethod
    def safe_id(task_id: str) -> str:
        return str(UUID(task_id))

    @staticmethod
    def snapshot_json(session: RuntimeStoredAggregate) -> str:
        return json.dumps(session.model_dump(mode="json"), ensure_ascii=False, indent=2)

    @staticmethod
    def capture_mutable_fields(
        session: RuntimeStoredAggregate,
    ) -> tuple[int, dict[str, int | None]]:
        return session.revision, {event.id: event.sequence for event in session.events}

    @staticmethod
    def restore_mutable_fields(
        session: RuntimeStoredAggregate,
        revision: int,
        sequences: dict[str, int | None],
    ) -> None:
        session.revision = revision
        for event in session.events:
            event.sequence = sequences.get(event.id)

    def task_exists(self, connection: sqlite3.Connection, task_id: str) -> bool:
        return (
            connection.execute(
                f"SELECT 1 FROM {self.layout.tasks} WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            is not None
        )

    def read_json_records(
        self,
        task_id: str,
        *,
        table: str,
        json_column: str,
        order_by: tuple[str, ...],
    ) -> list[str]:
        """Read ordered JSON records from a trusted domain table."""

        for identifier in (table, json_column, *order_by):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
                raise ValueError(f"Unsafe SQLite identifier: {identifier}")
        ordering = ", ".join(order_by)
        if not ordering:
            raise ValueError("At least one SQLite ordering column is required.")
        safe_id = self.safe_id(task_id)
        with self.connect() as connection:
            if not self.task_exists(connection, safe_id):
                raise KeyError(f"Unknown {self.layout.label} session: {safe_id}")
            rows = connection.execute(
                f"SELECT {json_column} FROM {table} "
                f"WHERE task_id = ? ORDER BY {ordering}",
                (safe_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def append_events(
        self,
        connection: sqlite3.Connection,
        session: RuntimeStoredAggregate,
        corruption: type[Exception],
    ) -> None:
        """Append new events and reject mutation of previously persisted facts."""

        table = self.layout.events
        row = connection.execute(
            f"SELECT COALESCE(MAX(sequence), 0) FROM {table} WHERE task_id = ?",
            (session.id,),
        ).fetchone()
        next_sequence = int(row[0]) + 1
        for event in session.events:
            existing = connection.execute(
                f"SELECT task_id, sequence, event_json FROM {table} WHERE event_id = ?",
                (event.id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise corruption(f"Event id {event.id} already belongs to another task.")
                stored = AgentEvent.model_validate_json(existing[2])
                candidate = event.model_copy(update={"sequence": int(existing[1])})
                if candidate != stored:
                    raise corruption(f"Persisted event {event.id} was modified after append.")
                event.sequence = int(existing[1])
                continue
            if event.sequence is not None:
                raise corruption(
                    f"Unpersisted event {event.id} already has sequence {event.sequence}."
                )
            event.sequence = next_sequence
            payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
            if self.layout.detailed_events:
                connection.execute(
                    f"""
                    INSERT INTO {table}(
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
                        payload,
                    ),
                )
            else:
                connection.execute(
                    f"""
                    INSERT INTO {table}(
                        task_id, sequence, event_id, event_type, timestamp, event_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.id,
                        next_sequence,
                        event.id,
                        event.type.value,
                        event.created_at.isoformat(),
                        payload,
                    ),
                )
            next_sequence += 1

    def upsert_runs(
        self,
        connection: sqlite3.Connection,
        session: RuntimeStoredAggregate,
        corruption: type[Exception],
    ) -> None:
        """Insert active runs and allow only their monotonic transition to a terminal state."""

        table = self.layout.runs
        for run in session.runs:
            existing = connection.execute(
                f"SELECT task_id, run_json FROM {table} WHERE run_id = ?",
                (run.id,),
            ).fetchone()
            payload = json.dumps(run.model_dump(mode="json"), ensure_ascii=False)
            if existing is None:
                if self.layout.detailed_runs:
                    connection.execute(
                        f"""
                        INSERT INTO {table}(
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
                            payload,
                        ),
                    )
                else:
                    connection.execute(
                        f"""
                        INSERT INTO {table}(
                            run_id, task_id, status, started_at, heartbeat_at, run_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.id,
                            session.id,
                            run.status.value,
                            run.started_at.isoformat(),
                            run.heartbeat_at.isoformat(),
                            payload,
                        ),
                    )
                continue
            if existing[0] != session.id:
                raise corruption(f"Run id {run.id} already belongs to another task.")
            stored = RuntimeRunRecord.model_validate_json(existing[1])
            if stored.status != RunStatus.STARTED and stored != run:
                raise corruption(
                    f"Persisted terminal run {run.id} was modified after completion."
                )
            if stored.status != RunStatus.STARTED:
                continue
            immutable_fields = (
                "task_id",
                "state",
                "mode",
                "owner_id",
                "owner_pid",
                "started_at",
            )
            if any(getattr(stored, field) != getattr(run, field) for field in immutable_fields):
                raise corruption(f"Persisted run {run.id} changed immutable identity fields.")
            if run.heartbeat_at < stored.heartbeat_at:
                raise corruption(f"Run {run.id} heartbeat moved backwards.")
            if self.layout.detailed_runs:
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET status = ?, heartbeat_at = ?, completed_at = ?, run_json = ?
                    WHERE run_id = ?
                    """,
                    (
                        run.status.value,
                        run.heartbeat_at.isoformat(),
                        run.completed_at.isoformat() if run.completed_at else None,
                        payload,
                        run.id,
                    ),
                )
            else:
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET status = ?, heartbeat_at = ?, run_json = ?
                    WHERE run_id = ?
                    """,
                    (run.status.value, run.heartbeat_at.isoformat(), payload, run.id),
                )

    def append_command_receipts(
        self,
        connection: sqlite3.Connection,
        session: RuntimeStoredAggregate,
        corruption: type[Exception],
    ) -> None:
        """Persist idempotency receipts as immutable command outcomes."""

        table = self.layout.commands
        for receipt in session.command_receipts:
            existing = connection.execute(
                f"SELECT task_id, receipt_json FROM {table} WHERE command_id = ?",
                (receipt.command_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != session.id:
                    raise corruption(
                        f"Command id {receipt.command_id} already belongs to another task."
                    )
                if CommandReceipt.model_validate_json(existing[1]) != receipt:
                    raise corruption(
                        f"Persisted command receipt {receipt.command_id} was modified."
                    )
                continue
            payload = json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False)
            if self.layout.detailed_commands:
                connection.execute(
                    f"""
                    INSERT INTO {table}(
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
                        payload,
                    ),
                )
            else:
                connection.execute(
                    f"""
                    INSERT INTO {table}(command_id, task_id, created_at, receipt_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (receipt.command_id, session.id, receipt.created_at.isoformat(), payload),
                )

    @staticmethod
    def replay_state(
        events: list[AgentEvent],
        corruption: type[Exception],
        *,
        label: str,
    ) -> TaskState:
        """Rebuild task state from transition facts and reject a broken event chain."""

        current = TaskState.CREATED
        for event in events:
            if event.type != EventType.STATE_TRANSITIONED:
                continue
            try:
                source = TaskState(str(event.data["from"]))
                target = TaskState(str(event.data["to"]))
            except (KeyError, ValueError) as exc:
                raise corruption(
                    f"{label.capitalize()} state event {event.id} has an invalid "
                    "transition payload."
                ) from exc
            if source != current:
                raise corruption(
                    f"{label.capitalize()} state event {event.id} expected {source.value}, "
                    f"replay is currently {current.value}."
                )
            current = target
        return current
