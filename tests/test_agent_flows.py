"""Deterministic acceptance tests for the stateful Agent kernel."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest
from pydantic import ValidationError

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.adapters.json_session_store import JsonSessionStore
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
    ChangeProposal,
    EventType,
    ProposedChange,
    RuntimeResult,
    RuntimeTurn,
)
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.database_models import DataQuery, QueryResult


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


class FakeDatabase:
    def __init__(self) -> None:
        self.queries: list[DataQuery] = []

    def describe_schema(self) -> str:
        return "orders(id INTEGER, status TEXT)"

    def execute(self, query: DataQuery) -> QueryResult:
        self.queries.append(query)
        return QueryResult(
            query_name=query.name,
            columns=["id", "status"],
            rows=[{"id": 42, "status": "stuck"}],
            returned_rows=1,
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
    proposal = (
        ChangeProposal(
            summary="Make the upload write atomic without changing the public API.",
            changes=[
                ProposedChange(
                    path="src/upload_service.py",
                    area="upload persistence",
                    current="The two related writes can succeed independently.",
                    proposed="Write both records within one transaction boundary.",
                )
            ],
            expected_result="A failed second write cannot leave partial upload state.",
            impact=["The public upload response remains unchanged."],
            validation=["Exercise success and second-write failure cases."],
            preview_markdown="`save_primary()` and `save_metadata()` run in one transaction.",
        )
        if scope == ApprovalScope.MODIFY
        else None
    )
    return AgentDecision(
        status=AgentStatus.APPROVAL_REQUIRED,
        message=f"Approval is needed for {scope.value}.",
        approval=ApprovalRequest(
            scope=scope,
            reason="The next step has a side effect.",
            proposed_actions=["Perform the exact requested action."],
            proposal=proposal,
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

    first = app.start(workspace, "Please optimize this project.", project="生物")
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
    assert final.task_state == TaskState.COMPLETED
    state_transitions = [
        (event.data["from"], event.data["to"])
        for event in final.events
        if event.type == EventType.STATE_TRANSITIONED
    ]
    assert state_transitions == [
        ("created", "inspecting"),
        ("inspecting", "waiting_input"),
        ("waiting_input", "inspecting"),
        ("inspecting", "waiting_input"),
        ("waiting_input", "inspecting"),
        ("inspecting", "completed"),
    ]
    assert app.get_session(first.session_id).project == "生物"
    assert "selected the knowledge project '生物'" in runtime.turns[0].system_prompt


def test_development_flow_can_use_shared_read_only_database(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    state = tmp_path / "state"
    query = DataQuery(
        name="order_status",
        purpose="Confirm the persisted order state before proposing a code change.",
        sql="SELECT id, status FROM orders WHERE id = :id",
        parameters={"id": 42},
    )
    runtime = ScriptedRuntime(
        AgentDecision(
            status=AgentStatus.QUERY_REQUIRED,
            message="The code path is located; one bounded data check is needed.",
            queries=[query],
        ),
        _completed("The persisted state confirms the code-path diagnosis."),
    )
    database = FakeDatabase()
    reference = "sqlserver://sql.internal:1433/orders"
    app = build_application(
        settings=_settings(state),
        runtime=runtime,
        database=database,
        database_reference=reference,
    )

    outcome = app.start(workspace, "Investigate order 42 in src/orders.py")

    assert outcome.status == AgentStatus.COMPLETED
    assert outcome.task_state == TaskState.COMPLETED
    assert database.queries == [query]
    assert outcome.query_observations[0].returned_rows == 1
    assert len(runtime.turns) == 2
    assert "orders(id INTEGER, status TEXT)" in runtime.turns[0].system_prompt
    assert "Never ask the user to run SQL" in " ".join(
        runtime.turns[0].system_prompt.split()
    )
    assert '"status": "stuck"' in runtime.turns[1].user_message
    session = app.get_session(outcome.session_id)
    assert session.database_reference == reference
    assert '"status":"stuck"' not in session.model_dump_json()
    assert all(
        message.content != "The code path is located; one bounded data check is needed."
        for message in session.messages
    )
    assert any("user does not need to run SQL" in message.content for message in session.messages)
    assert session.query_observations[0].sql_fingerprint is not None
    assert (state / "runtime" / "agent-runtime.db").is_file()
    assert [
        event.data["to"] for event in outcome.events if event.type == EventType.STATE_TRANSITIONED
    ] == ["inspecting", "querying_data", "inspecting", "completed"]
    assert outcome.capability_document is not None
    assert Path(outcome.capability_document).parent.parent.name == "development"


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
    assert pending.task_state == (
        TaskState.WAITING_MODIFY_APPROVAL
        if scope == ApprovalScope.MODIFY
        else TaskState.WAITING_VERIFY_APPROVAL
    )
    assert completed.status == AgentStatus.COMPLETED
    assert completed.task_state == TaskState.COMPLETED
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


def test_modify_approval_requires_a_structured_proposal(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = ScriptedRuntime(
        AgentDecision(
            status=AgentStatus.APPROVAL_REQUIRED,
            message="I want to edit the file now.",
            approval=ApprovalRequest(
                scope=ApprovalScope.MODIFY,
                reason="A code change is needed.",
            ),
        )
    )
    app = _app(tmp_path / "state", runtime)

    outcome = app.start(workspace, "Fix the upload bug.")

    assert outcome.status == AgentStatus.FAILED
    assert outcome.approval is None
    assert "must include the change proposal" in outcome.message


def test_modify_proposal_is_shown_before_implementation_and_survives_approval(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    pending_decision = _approval(ApprovalScope.MODIFY)
    runtime = ScriptedRuntime(pending_decision, _completed(changed=True))
    app = _app(tmp_path / "state", runtime)

    pending = app.start(workspace, "Fix the upload consistency bug.")

    assert pending.status == AgentStatus.APPROVAL_REQUIRED
    assert pending.approval is not None
    assert pending.approval.proposal == pending_decision.approval.proposal
    assert runtime.turns[0].mode == AgentMode.INSPECT
    assert "Edit" not in runtime.turns[0].tools

    completed = app.approve(pending.session_id)
    assert completed.status == AgentStatus.COMPLETED
    assert runtime.turns[1].mode == AgentMode.IMPLEMENT
    assert "exact proposal" in runtime.turns[1].user_message


def test_legacy_modify_approval_loads_but_cannot_bypass_proposal_review(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    legacy_approval = ApprovalRequest(
        scope=ApprovalScope.MODIFY,
        reason="Legacy edit request.",
        proposed_actions=["Edit the file."],
    )
    legacy_decision = AgentDecision(
        status=AgentStatus.APPROVAL_REQUIRED,
        message="Legacy approval.",
        approval=legacy_approval,
    )
    session = AgentSession(
        workspace=str(workspace),
        goal="Legacy task",
        status=AgentStatus.APPROVAL_REQUIRED,
        pending_approval=legacy_approval,
        last_decision=legacy_decision,
    )
    payload = session.model_dump(mode="json")
    payload["pending_approval"].pop("proposal")
    payload["last_decision"]["approval"].pop("proposal")
    restored = AgentSession.model_validate(payload)
    assert restored.pending_approval is not None
    assert restored.pending_approval.proposal is None
    JsonSessionStore(state).create(restored)
    app = _app(state, ScriptedRuntime(_completed(changed=True)))

    with pytest.raises(ValueError, match="predates change proposals"):
        app.approve(restored.id)


def test_change_proposal_rejects_blank_required_explanations() -> None:
    with pytest.raises(ValidationError):
        ChangeProposal(
            summary="   ",
            changes=[
                ProposedChange(
                    area="upload",
                    current="separate writes",
                    proposed="one transaction",
                )
            ],
            expected_result="consistent data",
        )


@pytest.mark.parametrize("unsafe_path", [r"C:src\upload.py", r"\src\upload.py"])
def test_change_proposal_rejects_windows_paths_outside_workspace(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    decision = _approval(ApprovalScope.MODIFY)
    assert decision.approval is not None
    assert decision.approval.proposal is not None
    decision.approval.proposal.changes[0].path = unsafe_path
    app = _app(tmp_path / "state", ScriptedRuntime(decision))

    outcome = app.start(workspace, "Fix the upload bug.")

    assert outcome.status == AgentStatus.FAILED
    assert "out-of-workspace path" in outcome.message


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
    assert restarted_app.get_session(first.session_id).task_state == TaskState.COMPLETED


def test_duplicate_approval_command_id_does_not_repeat_runtime(tmp_path: Path) -> None:
    workspace = tmp_path / "repo-idempotent"
    workspace.mkdir()
    runtime = ScriptedRuntime(_approval(ApprovalScope.MODIFY), _completed(changed=True))
    app = _app(tmp_path / "state-idempotent", runtime)
    pending = app.start(workspace, "Fix the upload consistency bug.")

    first = app.approve(pending.session_id, command_id="approve-command-1")
    duplicate = app.approve(pending.session_id, command_id="approve-command-1")
    session = app.get_session(pending.session_id)

    assert first.status == duplicate.status == AgentStatus.COMPLETED
    assert len(runtime.turns) == 2
    assert [item.command_id for item in session.command_receipts].count("approve-command-1") == 1


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
    workspace_memory = next((state / "workspaces").iterdir()) / "development"
    assert list((workspace_memory / "capabilities").glob("*.md")) == []
    assert list((workspace_memory / "tasks").glob("*.json")) == []
    assert "No completed-task capabilities yet." in (
        workspace_memory / "CAPABILITIES.md"
    ).read_text(encoding="utf-8")
