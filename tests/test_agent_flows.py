"""Deterministic acceptance tests for the stateful Agent kernel."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.application import AgentApplication, build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import (
    AgentDecision,
    AgentMode,
    AgentSession,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    CapabilityDraft,
    EventType,
    RuntimeResult,
    RuntimeTurn,
)


class ScriptedRuntime:
    """Return predetermined decisions while retaining every host/runtime contract."""

    def __init__(self, *decisions: AgentDecision, runtime_session_id: str = "runtime-1") -> None:
        self._decisions = deque(decisions)
        self.runtime_session_id = runtime_session_id
        self.turns: list[RuntimeTurn] = []

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        self.turns.append(turn.model_copy(deep=True))
        if not self._decisions:
            raise AssertionError("The test did not script a decision for this runtime turn.")
        return RuntimeResult(
            decision=self._decisions.popleft(),
            runtime_session_id=self.runtime_session_id,
        )


def _settings(data_dir: Path) -> Settings:
    return Settings(
        claude_command="claude-test.exe",
        claude_model="test-model",
        claude_timeout_seconds=30,
        data_dir=data_dir,
        max_budget_usd=None,
    )


def _app(data_dir: Path, runtime: ScriptedRuntime) -> AgentApplication:
    return build_application(settings=_settings(data_dir), runtime=runtime)


def _capability(title: str = "Trace upload consistency") -> CapabilityDraft:
    return CapabilityDraft(
        title=title,
        summary="Trace both writes before choosing a transaction boundary.",
        triggers=["One of two related writes is missing."],
        method=["Trace the caller and both persistence operations."],
        validation=["Exercise the partial-failure case."],
        risks=["Do not assume both stores share one transaction."],
    )


def _completed(message: str = "Task completed.", *, changed: bool = False) -> AgentDecision:
    return AgentDecision(
        status=AgentStatus.COMPLETED,
        message=message,
        changed_files=["src/upload_service.py"] if changed else [],
        test_summary="Focused test passed." if changed else None,
        capability=_capability(),
    )


def _approval(scope: ApprovalScope) -> AgentDecision:
    return AgentDecision(
        status=AgentStatus.APPROVAL_REQUIRED,
        message=f"Approval is needed for {scope.value}.",
        approval=ApprovalRequest(
            scope=scope,
            reason="The next step has a side effect.",
            proposed_actions=["Perform the exact requested action."],
        ),
    )


def test_multi_turn_clarification_preserves_full_history(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = ScriptedRuntime(
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="Which behavior is wrong?"),
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="Which endpoint shows it?"),
        _completed(),
    )
    app = _app(tmp_path / "state", runtime)

    first = app.start(workspace, "Please optimize this project.")
    second = app.send(first.session_id, "The upload behavior is wrong.")
    final = app.send(first.session_id, "POST /upload succeeds but testMod is missing.")

    assert [first.status, second.status, final.status] == [
        AgentStatus.NEEDS_INPUT,
        AgentStatus.NEEDS_INPUT,
        AgentStatus.COMPLETED,
    ]
    assert runtime.turns[0].history == []
    assert [message.content for message in runtime.turns[1].history] == [
        "Please optimize this project.",
        "Which behavior is wrong?",
    ]
    assert [message.content for message in runtime.turns[2].history] == [
        "Please optimize this project.",
        "Which behavior is wrong?",
        "The upload behavior is wrong.",
        "Which endpoint shows it?",
    ]
    assert all(turn.runtime_session_id == "runtime-1" for turn in runtime.turns[1:])
    event_types = [event.type for event in final.events]
    assert event_types.count(EventType.INPUT_REQUIRED) == 2
    assert event_types.count(EventType.TASK_COMPLETED) == 1
    assert event_types.count(EventType.CAPABILITY_SAVED) == 1


@pytest.mark.parametrize(
    ("scope", "expected_mode", "required_tool"),
    [
        (ApprovalScope.MODIFY, AgentMode.IMPLEMENT, "Edit"),
        (ApprovalScope.VERIFY, AgentMode.VERIFY, "Bash"),
    ],
)
def test_approval_resumes_in_exact_authorized_mode(
    tmp_path: Path,
    scope: ApprovalScope,
    expected_mode: AgentMode,
    required_tool: str,
) -> None:
    workspace = tmp_path / f"repo-{scope.value}"
    workspace.mkdir()
    final_decision = _completed(changed=scope == ApprovalScope.MODIFY)
    runtime = ScriptedRuntime(_approval(scope), final_decision)
    app = _app(tmp_path / f"state-{scope.value}", runtime)

    pending = app.start(workspace, "Fix the upload consistency bug.")
    completed = app.approve(pending.session_id)

    assert pending.status == AgentStatus.APPROVAL_REQUIRED
    assert completed.status == AgentStatus.COMPLETED
    assert [turn.mode for turn in runtime.turns] == [AgentMode.INSPECT, expected_mode]
    assert required_tool in runtime.turns[1].tools
    assert runtime.turns[0].capability_dir is not None
    assert runtime.turns[1].capability_dir is None
    assert runtime.turns[1].runtime_session_id == "runtime-1"
    assert "approved" in runtime.turns[1].user_message.lower()
    assert completed.approval is None


def test_rejection_resumes_read_only_and_carries_reason(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = ScriptedRuntime(
        _approval(ApprovalScope.MODIFY),
        AgentDecision(
            status=AgentStatus.NEEDS_INPUT,
            message="I will continue without modifying files.",
        ),
    )
    app = _app(tmp_path / "state", runtime)

    pending = app.start(workspace, "Fix the issue.")
    outcome = app.reject(pending.session_id, "I only want a diagnosis")

    rejected_turn = runtime.turns[1]
    assert outcome.status == AgentStatus.NEEDS_INPUT
    assert outcome.approval is None
    assert rejected_turn.mode == AgentMode.INSPECT
    assert "Edit" not in rejected_turn.tools
    assert "Write" not in rejected_turn.tools
    assert "I only want a diagnosis" in rejected_turn.user_message
    assert rejected_turn.runtime_session_id == "runtime-1"


def test_session_can_resume_after_application_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = _settings(tmp_path / "state")
    first_runtime = ScriptedRuntime(
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="Provide the failing endpoint."),
        runtime_session_id="claude-session-42",
    )
    first_app = build_application(settings=settings, runtime=first_runtime)
    first = first_app.start(workspace, "Investigate an upload failure.")

    second_runtime = ScriptedRuntime(_completed(), runtime_session_id="claude-session-42")
    restarted_app = build_application(settings=settings, runtime=second_runtime)
    final = restarted_app.send(first.session_id, "The endpoint is POST /upload.")

    resumed = second_runtime.turns[0]
    assert final.session_id == first.session_id
    assert final.status == AgentStatus.COMPLETED
    assert resumed.runtime_session_id == "claude-session-42"
    assert [message.content for message in resumed.history] == [
        "Investigate an upload failure.",
        "Provide the failing endpoint.",
    ]
    assert restarted_app.get_session(first.session_id).status == AgentStatus.COMPLETED


def test_first_turn_failure_preserves_preallocated_runtime_session(tmp_path: Path) -> None:
    class FailingRuntime:
        def run(self, turn: RuntimeTurn) -> RuntimeResult:
            assert turn.runtime_session_id is None
            raise RuntimeError("provider unavailable")

    workspace = tmp_path / "repo"
    workspace.mkdir()
    app = build_application(settings=_settings(tmp_path / "state"), runtime=FailingRuntime())

    outcome = app.start(workspace, "Inspect the failure.")
    session = app.get_session(outcome.session_id)

    assert outcome.status == AgentStatus.FAILED
    assert session.runtime_session_id == session.id


def test_completed_capability_is_idempotent_and_redacts_sensitive_data(tmp_path: Path) -> None:
    workspace = tmp_path / "private-repo"
    workspace.mkdir()
    secret = "supersecret123"
    session = AgentSession(
        workspace=str(workspace.resolve()),
        goal=f"Inspect {workspace.resolve()} api_key={secret} Bearer {secret}",
    )
    decision = AgentDecision(
        status=AgentStatus.COMPLETED,
        message=(
            f"Completed in {workspace.resolve()} with password={secret} and "
            f"https://user:{secret}@example.invalid/repo"
        ),
        capability=CapabilityDraft(
            title=f"Upload diagnosis token={secret}",
            summary=f"Authorization: Bearer {secret}",
            triggers=[f"Secret={secret}"],
            method=[f"Read {workspace.resolve() / 'src' / 'upload.py'}"],
            validation=["pytest passed"],
            risks=[f"Do not reveal api-key={secret}"],
        ),
    )
    store = CapabilityStore(tmp_path / "agent-data")

    first = store.record(session, decision, "test-model")
    original = Path(first.document_path).read_text(encoding="utf-8")
    second = store.record(session, decision, "test-model")

    workspace_dir = Path(first.index_path).parent
    persisted_text = "\n".join(
        path.read_text(encoding="utf-8") for path in workspace_dir.rglob("*") if path.is_file()
    )
    persisted_names = "\n".join(path.name for path in workspace_dir.rglob("*"))
    assert first.created is True
    assert second.created is False
    assert second.document_path == first.document_path
    assert Path(second.document_path).read_text(encoding="utf-8") == original
    assert len(list((workspace_dir / "capabilities").glob("*.md"))) == 1
    assert len(list((workspace_dir / "tasks").glob("*.json"))) == 1
    assert secret not in persisted_text
    assert secret not in persisted_names
    assert str(workspace.resolve()) not in persisted_text
    assert "[REDACTED]" in persisted_text
    assert "<WORKSPACE>" in persisted_text


@pytest.mark.parametrize(
    "decision",
    [
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="Which file is involved?"),
        _approval(ApprovalScope.MODIFY),
        AgentDecision(status=AgentStatus.FAILED, message="Runtime failed."),
    ],
    ids=["needs-input", "approval-required", "failed"],
)
def test_non_completed_outcomes_do_not_write_capabilities(
    tmp_path: Path,
    decision: AgentDecision,
) -> None:
    case = decision.status.value
    workspace = tmp_path / f"repo-{case}"
    workspace.mkdir()
    state = tmp_path / f"state-{case}"
    app = _app(state, ScriptedRuntime(decision))

    outcome = app.start(workspace, "Handle this task.")

    assert outcome.status == decision.status
    assert outcome.capability_document is None
    workspace_memory = next((state / "workspaces").iterdir())
    assert list((workspace_memory / "capabilities").glob("*.md")) == []
    assert list((workspace_memory / "tasks").glob("*.json")) == []
    assert "No completed-task capabilities yet." in (
        workspace_memory / "CAPABILITIES.md"
    ).read_text(encoding="utf-8")
