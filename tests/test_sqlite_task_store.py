"""Transactional task snapshot, event replay, and migration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocoding_agent.adapters.json_session_store import JsonSessionStore
from autocoding_agent.adapters.sqlite_task_store import (
    ConcurrentSessionUpdate,
    EventStoreCorruption,
    SQLiteTaskStore,
)
from autocoding_agent.core.artifacts.models import ArtifactRecord, ArtifactType
from autocoding_agent.core.audit.models import DecisionRecord, RiskLevel
from autocoding_agent.core.models import AgentEvent, AgentSession, EventType
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import AgentStateMachine
from autocoding_agent.core.state_machine.models import TaskState


def _session(tmp_path: Path) -> AgentSession:
    session = AgentSession(workspace=str(tmp_path), goal="Persist the lifecycle.")
    session.events.append(
        AgentEvent(
            type=EventType.TASK_CREATED,
            message="Created task.",
            actor="user",
        )
    )
    AgentStateMachine().transition(
        session,
        TaskState.INSPECTING,
        reason="Begin inspection.",
        command_id="command-1",
    )
    return session


def test_create_assigns_sequences_and_replays_snapshot_state(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)

    store.create(session)

    restored = store.load(session.id)
    events = store.list_events(session.id)
    assert session.revision == 1
    assert restored.revision == 1
    assert [event.sequence for event in events] == [1, 2]
    assert [event.id for event in restored.events] == [event.id for event in events]
    assert store.replay_task_state(session.id) == TaskState.INSPECTING
    assert restored.task_state == store.replay_task_state(session.id)


def test_save_appends_only_new_events_and_advances_revision(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    store.create(session)
    AgentStateMachine().transition(
        session,
        TaskState.WAITING_INPUT,
        reason="Need one answer.",
        command_id="command-1",
    )

    store.save(session)

    assert session.revision == 2
    assert [event.sequence for event in store.list_events(session.id)] == [1, 2, 3]
    assert store.replay_task_state(session.id) == TaskState.WAITING_INPUT


def test_concurrent_stale_snapshot_is_rejected(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    store.create(session)
    first = store.load(session.id)
    stale = store.load(session.id)
    AgentStateMachine().transition(
        first,
        TaskState.WAITING_INPUT,
        reason="Need one answer.",
    )
    store.save(first)
    AgentStateMachine().transition(
        stale,
        TaskState.WAITING_INPUT,
        reason="Stale caller also needs input.",
    )

    with pytest.raises(ConcurrentSessionUpdate, match="changed"):
        store.save(stale)

    assert store.load(session.id).revision == 2
    assert store.replay_task_state(session.id) == TaskState.WAITING_INPUT


def test_appended_event_is_immutable(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    store.create(session)
    session.events[0].message = "Tampered after append."

    with pytest.raises(EventStoreCorruption, match="modified after append"):
        store.save(session)

    assert store.load(session.id).events[0].message == "Created task."


def test_snapshot_failure_rolls_back_events_and_in_memory_sequences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    monkeypatch.setattr(
        store,
        "_snapshot_json",
        lambda _session: (_ for _ in ()).throw(OSError("snapshot failed")),
    )

    with pytest.raises(OSError, match="snapshot failed"):
        store.create(session)

    assert session.revision == 0
    assert all(event.sequence is None for event in session.events)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_legacy_json_is_imported_with_replayable_synthetic_transition(
    tmp_path: Path,
) -> None:
    legacy = AgentSession(
        workspace=str(tmp_path),
        goal="Resume an old approval.",
        status="needs_input",
    )
    JsonSessionStore(tmp_path).create(legacy)

    store = SQLiteTaskStore(tmp_path)
    restored = store.load(legacy.id)

    assert restored.task_state == TaskState.WAITING_INPUT
    assert store.replay_task_state(legacy.id) == TaskState.WAITING_INPUT
    assert [event.actor for event in store.list_events(legacy.id)[:2]] == [
        "migration",
        "migration",
    ]


def test_replay_detects_a_broken_transition_chain(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    store.create(session)
    events = store.list_events(session.id)
    broken = events[1].model_copy(deep=True)
    broken.data["from"] = TaskState.VERIFYING.value
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE events SET event_json = ? WHERE event_id = ?",
            (broken.model_dump_json(), broken.id),
        )
        connection.commit()

    with pytest.raises(EventStoreCorruption, match="replay is currently created"):
        store.replay_task_state(session.id)


def test_decisions_and_artifacts_are_queryable_immutable_records(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    decision_event = AgentEvent(
        type=EventType.DECISION_RECORDED,
        message="Recorded decision.",
    )
    artifact_event = AgentEvent(
        type=EventType.ARTIFACT_RECORDED,
        message="Recorded artifact.",
    )
    session.events.extend([decision_event, artifact_event])
    decision = DecisionRecord(
        task_id=session.id,
        event_id=decision_event.id,
        decision_type="approval_required",
        summary="Change one service.",
        reason="The transaction boundary is missing.",
        confidence=0.86,
        risk_level=RiskLevel.HIGH,
        model="test-model",
    )
    artifact = ArtifactRecord(
        task_id=session.id,
        event_id=artifact_event.id,
        type=ArtifactType.PROPOSAL,
        relative_path=f"tasks/{session.id}/artifacts/proposal.json",
        sha256="0" * 64,
        size_bytes=20,
        source="model_proposal",
    )
    session.decision_records.append(decision)
    session.artifacts.append(artifact)

    store.create(session)

    assert store.list_decisions(session.id) == [decision]
    assert store.list_artifacts(session.id) == [artifact]

    session.decision_records[0].reason = "Tampered rationale."
    with pytest.raises(EventStoreCorruption, match="decision .* modified"):
        store.save(session)


def test_invalid_artifact_event_reference_rolls_back_whole_create(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    session.artifacts.append(
        ArtifactRecord(
            task_id=session.id,
            event_id="missing-event",
            type=ArtifactType.CONTEXT,
            relative_path=f"tasks/{session.id}/artifacts/context.json",
            sha256="0" * 64,
            size_bytes=2,
            source="host",
        )
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.create(session)

    assert session.revision == 0
    assert all(event.sequence is None for event in session.events)
    with sqlite3.connect(store.path) as connection:
        for table in ("tasks", "events", "decisions", "artifacts"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_runtime_run_can_advance_once_and_is_immutable_after_terminal(
    tmp_path: Path,
) -> None:
    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    run = RuntimeRunRecord(
        task_id=session.id,
        state=TaskState.INSPECTING,
        mode="inspect",
    )
    session.runs.append(run)
    store.create(session)

    run.status = RunStatus.COMPLETED
    run.completed_at = run.heartbeat_at
    store.save(session)

    assert store.list_runs(session.id) == [run]
    run.terminal_reason = "Tampered after completion."
    with pytest.raises(EventStoreCorruption, match="terminal run .* modified"):
        store.save(session)


def test_command_receipt_is_persisted_with_global_idempotency_key(tmp_path: Path) -> None:
    from autocoding_agent.core.state_machine.models import AgentCommandType, CommandReceipt

    store = SQLiteTaskStore(tmp_path)
    session = _session(tmp_path)
    receipt = CommandReceipt(
        command_id="command-receipt-1",
        task_id=session.id,
        command_type=AgentCommandType.SUBMIT_USER_INPUT,
        outcome_status="needs_input",
        outcome_message="Need one input.",
        task_state=TaskState.WAITING_INPUT,
        completed_version=2,
    )
    session.command_receipts.append(receipt)

    store.create(session)

    assert store.list_command_receipts(session.id) == [receipt]
    receipt.outcome_message = "Tampered."
    with pytest.raises(EventStoreCorruption, match="command receipt .* modified"):
        store.save(session)
