"""Crash reconciliation, recovery choices, and bounded replanning tests."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autocoding_agent.adapters.sqlite_task_store import SQLiteTaskStore
from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.artifacts.models import ArtifactType
from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentSession,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    EventType,
    ProposedChange,
    RuntimeResult,
    RuntimeTurn,
)
from autocoding_agent.core.recovery.models import RecoveryAction
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import AgentStateMachine
from autocoding_agent.core.state_machine.models import TaskState


def _settings(data_dir: Path, *, max_replans: int = 2) -> Settings:
    return Settings(
        claude_command="claude-test.exe",
        claude_model="test-model",
        claude_timeout_seconds=30,
        data_dir=data_dir,
        max_budget_usd=None,
        runtime_lease_seconds=5,
        agent_max_replan_rounds=max_replans,
    )


def _orphaned_session(workspace: Path, state: TaskState) -> AgentSession:
    session = AgentSession(workspace=str(workspace), goal="Recover this task.")
    session.events.append(
        AgentEvent(type=EventType.TASK_CREATED, message="Created task.", actor="user")
    )
    machine = AgentStateMachine()
    machine.transition(session, TaskState.INSPECTING, reason="Start inspection.")
    if state == TaskState.IMPLEMENTING:
        machine.transition(
            session,
            TaskState.WAITING_MODIFY_APPROVAL,
            reason="Wait for modify approval.",
        )
        machine.transition(session, TaskState.IMPLEMENTING, reason="Approval granted.")
    elif state == TaskState.VERIFYING:
        machine.transition(
            session,
            TaskState.WAITING_VERIFY_APPROVAL,
            reason="Wait for verify approval.",
        )
        machine.transition(session, TaskState.VERIFYING, reason="Approval granted.")
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    session.runs.append(
        RuntimeRunRecord(
            task_id=session.id,
            state=state,
            mode={
                TaskState.INSPECTING: "inspect",
                TaskState.IMPLEMENTING: "implement",
                TaskState.VERIFYING: "verify",
            }[state],
            owner_id="dead-owner",
            owner_pid=99_999_999,
            started_at=old,
            heartbeat_at=old,
            runtime_session_id="runtime-before-crash",
        )
    )
    return session


class NeverRuntime:
    def run(self, _turn: RuntimeTurn) -> RuntimeResult:
        raise AssertionError("Startup recovery must not replay the Runtime.")


class CompleteRuntime:
    def __init__(self) -> None:
        self.turns: list[RuntimeTurn] = []

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        self.turns.append(turn)
        return RuntimeResult(
            decision=AgentDecision(status=AgentStatus.COMPLETED, message="Inspected safely."),
            runtime_session_id="runtime-after-recovery",
        )


class ScriptedRuntime:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = deque(decisions)

    def run(self, _turn: RuntimeTurn) -> RuntimeResult:
        return RuntimeResult(
            decision=self.decisions.popleft(),
            runtime_session_id="runtime-replan",
        )


class CrashAfterEditRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        self.calls += 1
        if self.calls == 1:
            return RuntimeResult(
                decision=AgentDecision(
                    status=AgentStatus.APPROVAL_REQUIRED,
                    message="Approve the exact edit.",
                    approval=ApprovalRequest(
                        scope=ApprovalScope.MODIFY,
                        reason="A file edit is required.",
                        proposal=ChangeProposal(
                            summary="Update the value.",
                            changes=[
                                ProposedChange(
                                    path="service.py",
                                    area="service value",
                                    current="value = 1",
                                    proposed="value = 2",
                                )
                            ],
                            expected_result="The service uses value 2.",
                        ),
                    ),
                ),
                runtime_session_id="runtime-crash",
            )
        Path(turn.workspace, "service.py").write_text("value = 2\n", encoding="utf-8")
        raise RuntimeError("provider connection dropped after edit")


def _verify_approval() -> AgentDecision:
    return AgentDecision(
        status=AgentStatus.APPROVAL_REQUIRED,
        message="Approve verification.",
        approval=ApprovalRequest(
            scope=ApprovalScope.VERIFY,
            reason="Run focused checks.",
            proposed_actions=["Run tests."],
            proposal=None,
        ),
    )


def test_startup_moves_orphaned_implementation_to_recovery_required(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    state = tmp_path / "state"
    session = _orphaned_session(workspace, TaskState.IMPLEMENTING)
    SQLiteTaskStore(state).create(session)

    app = build_application(settings=_settings(state), runtime=NeverRuntime())
    restored = app.get_session(session.id)

    assert app.recovery_scan.recovered_task_ids == [session.id]
    assert restored.task_state == TaskState.RECOVERY_REQUIRED
    assert restored.runs[0].status == RunStatus.INTERRUPTED
    assert any(item.type == ArtifactType.RECOVERY_REPORT for item in restored.artifacts)
    assert any(event.type == EventType.RECOVERY_REQUIRED for event in restored.events)
    assert SQLiteTaskStore(state).replay_task_state(session.id) == TaskState.RECOVERY_REQUIRED


def test_orphaned_inspection_pauses_then_resumes_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    state = tmp_path / "state"
    session = _orphaned_session(workspace, TaskState.INSPECTING)
    SQLiteTaskStore(state).create(session)
    runtime = CompleteRuntime()

    app = build_application(settings=_settings(state), runtime=runtime)
    paused = app.get_session(session.id)
    completed = app.resume(session.id, RecoveryAction.READ_ONLY_INSPECT)

    assert paused.task_state == TaskState.PAUSED
    assert completed.task_state == TaskState.COMPLETED
    assert len(runtime.turns) == 1
    assert runtime.turns[0].mode.value == "inspect"


def test_verification_failure_replans_then_stops_at_configured_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    failed = AgentDecision(status=AgentStatus.FAILED, message="Focused test failed.")
    runtime = ScriptedRuntime(
        [
            _verify_approval(),
            failed,
            _verify_approval(),
            failed,
            _verify_approval(),
            failed,
        ]
    )
    app = build_application(
        settings=_settings(tmp_path / "state", max_replans=2),
        runtime=runtime,
    )

    first = app.start(workspace, "Verify the behavior.")
    replan_one = app.approve(first.session_id)
    second = app.send(first.session_id, "Investigate the failed verification.")
    replan_two = app.approve(second.session_id)
    third = app.send(first.session_id, "Investigate once more.")
    terminal = app.approve(third.session_id)

    assert replan_one.task_state == TaskState.REPLANNING
    assert replan_two.task_state == TaskState.REPLANNING
    assert terminal.task_state == TaskState.FAILED
    assert app.get_session(first.session_id).replan_rounds == 2


def test_implementation_failure_after_edit_requires_recovery_without_retry(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "service.py").write_text("value = 1\n", encoding="utf-8")
    runtime = CrashAfterEditRuntime()
    app = build_application(settings=_settings(tmp_path / "state"), runtime=runtime)

    pending = app.start(workspace, "Update service.py.")
    interrupted = app.approve(pending.session_id)
    session = app.get_session(pending.session_id)

    assert interrupted.task_state == TaskState.RECOVERY_REQUIRED
    assert runtime.calls == 2
    assert (workspace / "service.py").read_text(encoding="utf-8") == "value = 2\n"
    assert any(item.type == ArtifactType.RECOVERY_REPORT for item in session.artifacts)
    assert any(item.type == ArtifactType.CHANGES_PATCH for item in session.artifacts)


def test_cancel_from_recovery_is_terminal_and_does_not_run_model(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    state = tmp_path / "state"
    session = _orphaned_session(workspace, TaskState.VERIFYING)
    SQLiteTaskStore(state).create(session)
    app = build_application(settings=_settings(state), runtime=NeverRuntime())

    cancelled = app.resume(session.id, RecoveryAction.CANCEL)

    assert cancelled.task_state == TaskState.CANCELLED
    assert app.get_session(session.id).task_state == TaskState.CANCELLED


def test_stop_request_pauses_observed_read_and_survives_restart(tmp_path):
    from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind

    workspace = tmp_path / "work"
    workspace.mkdir()
    data = tmp_path / "data"

    class StopRuntime:
        def run_observed(self, turn, run_id, event_sink):
            SQLiteTaskStore(data).database.request_stop(turn.session_id)
            event_sink(RuntimeActivity(
                run_id=run_id, kind=RuntimeEventKind.HEARTBEAT, summary="waiting",
            ))
            raise AssertionError("Stop must prevent further runtime work")

    app = build_application(settings=_settings(data), runtime=StopRuntime())
    outcome = app.start(workspace, "inspect")
    session = app.get_session(outcome.session_id)
    assert session.task_state == TaskState.PAUSED
    assert session.runs[-1].status == RunStatus.INTERRUPTED
    assert SQLiteTaskStore(data).database.stop_requested(session.id)
    runtime = CompleteRuntime()
    restarted = build_application(settings=_settings(data), runtime=runtime)
    assert runtime.turns == []
    assert restarted.get_session(session.id).messages == session.messages
    restarted.resume(session.id)
    assert len(runtime.turns) == 1
    assert runtime.turns[0].mode.value == "inspect"
    assert not SQLiteTaskStore(data).database.stop_requested(session.id)


def test_recovery_handles_crash_between_run_result_and_next_stage(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    session = _orphaned_session(workspace, TaskState.IMPLEMENTING)
    run = session.runs[-1]
    run.status = RunStatus.COMPLETED
    run.completed_at = datetime.now(timezone.utc)
    store = SQLiteTaskStore(tmp_path / "data")
    store.create(session)
    app = build_application(settings=_settings(tmp_path / "data"), runtime=NeverRuntime())
    recovered = app.get_session(session.id)
    assert recovered.task_state == TaskState.RECOVERY_REQUIRED
    assert recovered.runs[-1].status == RunStatus.COMPLETED
    assert recovered.runs[-1].completed_at == run.completed_at


def test_recovery_does_not_take_over_live_child_of_dead_owner(tmp_path):
    import os

    workspace = tmp_path / "work"
    workspace.mkdir()
    session = _orphaned_session(workspace, TaskState.IMPLEMENTING)
    session.runs[-1].runtime_pid = os.getpid()
    SQLiteTaskStore(tmp_path / "data").create(session)
    app = build_application(settings=_settings(tmp_path / "data"), runtime=NeverRuntime())
    assert app.get_session(session.id).task_state == TaskState.IMPLEMENTING
    assert session.runs[-1].id in app.recovery_scan.skipped_live_run_ids


def test_stop_during_write_waits_for_result_without_replaying(tmp_path):
    from autocoding_agent.core.models import AgentMode
    from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind

    workspace = tmp_path / "work"
    workspace.mkdir()
    data = tmp_path / "data"

    class WriteRuntime:
        writes = 0

        def run_observed(self, turn, run_id, event_sink):
            if turn.mode == AgentMode.INSPECT:
                return RuntimeResult(decision=AgentDecision(
                    status=AgentStatus.APPROVAL_REQUIRED, message="Approve edit",
                    approval=ApprovalRequest(
                        scope=ApprovalScope.MODIFY, reason="test",
                        proposal=ChangeProposal(
                            summary="edit", expected_result="file saved", changes=[
                            ProposedChange(path="a.txt", area="file", current="absent",
                                           proposed="write once"),
                        ]),
                    ),
                ), runtime_session_id=turn.session_id)
            self.writes += 1
            (workspace / "a.txt").write_text("saved", encoding="utf-8")
            SQLiteTaskStore(data).database.request_stop(turn.session_id)
            event_sink(RuntimeActivity(
                run_id=run_id, kind=RuntimeEventKind.TOOL_FINISHED,
                summary="edit finished", tool_name="Write", tool_use_id="write-1",
                data={"path": "a.txt", "is_error": False},
            ))
            return RuntimeResult(decision=AgentDecision(
                status=AgentStatus.COMPLETED, message="File saved", changed_files=["a.txt"],
            ), runtime_session_id=turn.session_id)

    runtime = WriteRuntime()
    app = build_application(settings=_settings(data), runtime=runtime)
    outcome = app.start(workspace, "edit file")
    app.approve(outcome.session_id)
    session = app.get_session(outcome.session_id)
    assert session.task_state == TaskState.PAUSED
    assert session.runs[-1].status == RunStatus.COMPLETED
    assert runtime.writes == 1
    assert (workspace / "a.txt").read_text() == "saved"
    assert any(m.content == "File saved" for m in session.messages)
    resumed = build_application(settings=_settings(data), runtime=CompleteRuntime())
    resumed.resume(session.id)
    assert runtime.writes == 1


def test_recovery_covers_host_context_preparation_before_first_model_run(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    session = AgentSession(workspace=str(workspace), goal="prepare")
    session.events.append(AgentEvent(
        type=EventType.TASK_CREATED, message="created",
        data={"owner_id": "dead", "owner_pid": 99_999_999},
    ))
    SQLiteTaskStore(tmp_path / "data").create(session)
    app = build_application(settings=_settings(tmp_path / "data"), runtime=NeverRuntime())
    assert app.get_session(session.id).task_state == TaskState.PAUSED


def test_recovery_context_marks_changed_files_and_unknown_operations(tmp_path):
    from autocoding_agent.core.recovery.context import recovery_context
    from autocoding_agent.core.workflow import read_fingerprint

    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="recover")
    session.events.extend([
        AgentEvent(type=EventType.TOOL_FINISHED, message="saved", correlation_id="r",
                   data={"tool_use_id": "1", "tool_name": "Write", "path": "a.txt",
                         "content_sha256": read_fingerprint(str(tmp_path), "a.txt")}),
        AgentEvent(type=EventType.TOOL_STARTED, message="started", correlation_id="r",
                   data={"tool_use_id": "2", "tool_name": "Bash"}),
    ])
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    context = recovery_context(session)
    assert '"unchanged": false' in context
    assert '"result": "unknown"' in context


def test_simultaneous_resume_only_starts_one_runtime(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from autocoding_agent.adapters.sqlite_task_store import ConcurrentSessionUpdate

    workspace = tmp_path / "work"
    workspace.mkdir()
    session = _orphaned_session(workspace, TaskState.INSPECTING)
    SQLiteTaskStore(tmp_path / "data").create(session)
    runtime = CompleteRuntime()
    app = build_application(settings=_settings(tmp_path / "data"), runtime=runtime)
    load = app._engine.sessions.load
    barrier = Barrier(2)

    def simultaneous_load(task_id):
        snapshot = load(task_id)
        barrier.wait(timeout=5)
        return snapshot

    monkeypatch.setattr(app._engine.sessions, "load", simultaneous_load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(app.resume, session.id) for _ in range(2)]
        errors = [future.exception(timeout=10) for future in futures]
    assert sum(isinstance(error, ConcurrentSessionUpdate) for error in errors) == 1
    assert sum(error is None for error in errors) == 1
    assert len(runtime.turns) == 1
