"""Artifact integrity, workspace evidence, and change-explanation tests."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from autocoding_agent.adapters.task_artifact_store import (
    ArtifactTooLarge,
    TaskArtifactStore,
)
from autocoding_agent.adapters.workspace_snapshot import GitWorkspaceObserver
from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.artifacts.models import ArtifactType
from autocoding_agent.core.audit.models import RiskLevel
from autocoding_agent.core.models import (
    AgentDecision,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    EventType,
    Evidence,
    ProposedChange,
    RuntimeResult,
    RuntimeTurn,
)
from autocoding_agent.core.runtime.models import RunStatus, RuntimeActivity, RuntimeEventKind


def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _initialize_repository(workspace: Path) -> Path:
    workspace.mkdir()
    source = workspace / "service.py"
    source.write_text("value = 1\n", encoding="utf-8")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "agent-tests@example.invalid")
    _git(workspace, "config", "user.name", "Agent Tests")
    _git(workspace, "add", "service.py")
    _git(workspace, "commit", "-qm", "baseline")
    return source


def _settings(data_dir: Path) -> Settings:
    return Settings(
        claude_command="claude-test.exe",
        claude_model="test-model",
        claude_timeout_seconds=30,
        data_dir=data_dir,
        max_budget_usd=None,
    )


class EditingRuntime:
    def __init__(self) -> None:
        self.turns = 0

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        self.turns += 1
        if self.turns == 1:
            decision = AgentDecision(
                status=AgentStatus.APPROVAL_REQUIRED,
                message="Approve the exact service update.",
                reason="The inspected branch returns the obsolete value.",
                confidence=0.88,
                risk_level=RiskLevel.HIGH,
                evidence=[Evidence(path="service.py", summary="The value is still 1.")],
                approval=ApprovalRequest(
                    scope=ApprovalScope.MODIFY,
                    reason="Repository modification requires approval.",
                    proposal=ChangeProposal(
                        summary="Update the service value.",
                        changes=[
                            ProposedChange(
                                path="service.py",
                                area="service value",
                                current="The value is 1.",
                                proposed="Set the value to 2.",
                            )
                        ],
                        expected_result="The service exposes value 2.",
                        validation=["Inspect the resulting diff."],
                    ),
                ),
            )
        else:
            Path(turn.workspace, "service.py").write_text("value = 2\n", encoding="utf-8")
            decision = AgentDecision(
                status=AgentStatus.COMPLETED,
                message="Updated the approved service value.",
                reason="The approved one-line change was applied.",
                confidence=0.95,
                risk_level=RiskLevel.LOW,
                evidence=[Evidence(path="service.py", summary="Host diff should show 1 to 2.")],
                changed_files=["service.py"],
                test_summary="The model inspected the resulting diff.",
            )
        return RuntimeResult(decision=decision, runtime_session_id="runtime-artifact-test")

    def run_observed(
        self,
        turn: RuntimeTurn,
        run_id: str,
        event_sink: Callable[[RuntimeActivity], None],
    ) -> RuntimeResult:
        if self.turns == 0:
            event_sink(
                RuntimeActivity(
                    run_id=run_id,
                    kind=RuntimeEventKind.TOOL_STARTED,
                    summary="Claude Code started Read.",
                    tool_name="Read",
                    tool_use_id="read-1",
                    data={"path": "service.py"},
                )
            )
            event_sink(
                RuntimeActivity(
                    run_id=run_id,
                    kind=RuntimeEventKind.TOOL_FINISHED,
                    summary="Claude Code finished Read.",
                    tool_name="Read",
                    tool_use_id="read-1",
                    data={"path": "service.py", "is_error": False},
                )
            )
        else:
            event_sink(
                RuntimeActivity(
                    run_id=run_id,
                    kind=RuntimeEventKind.TOOL_STARTED,
                    summary="Claude Code started Bash.",
                    tool_name="Bash",
                    tool_use_id="bash-1",
                    data={"command": "python -m pytest -q"},
                )
            )
            event_sink(
                RuntimeActivity(
                    run_id=run_id,
                    kind=RuntimeEventKind.TOOL_FINISHED,
                    summary="Claude Code finished Bash.",
                    tool_name="Bash",
                    tool_use_id="bash-1",
                    data={"command": "python -m pytest -q", "is_error": False},
                )
            )
        return self.run(turn)


def test_artifact_store_is_atomic_hashed_and_redacted(tmp_path: Path) -> None:
    store = TaskArtifactStore(tmp_path)
    task_id = str(uuid4())

    artifact = store.write_text(
        task_id=task_id,
        event_id=str(uuid4()),
        artifact_type=ArtifactType.CONTEXT,
        content="password=hunter2\nAuthorization: Bearer abc.def.ghi\nvalue=ok",
        source="test",
        host_verified=True,
    )

    path = Path(store.resolve_path(artifact))
    content = path.read_text(encoding="utf-8")
    manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert "hunter2" not in content
    assert "abc.def.ghi" not in content
    assert artifact.sensitive is True
    assert store.verify(artifact)
    assert manifest["artifacts"][0]["sha256"] == artifact.sha256
    assert not list(path.parent.glob(".tmp-*"))


def test_artifact_store_rejects_oversized_content(tmp_path: Path) -> None:
    store = TaskArtifactStore(tmp_path, max_bytes=1024)

    with pytest.raises(ArtifactTooLarge, match="maximum is 1024"):
        store.write_text(
            task_id=str(uuid4()),
            event_id=str(uuid4()),
            artifact_type=ArtifactType.CONTEXT,
            content="x" * 1025,
            source="test",
            host_verified=True,
        )


def test_workspace_observer_preserves_clean_baseline_and_current_diff(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    source = _initialize_repository(workspace)
    observer = GitWorkspaceObserver()

    baseline = observer.capture(workspace)
    source.write_text("value = 2\n", encoding="utf-8")
    (workspace / "untracked.txt").write_text("secret file contents", encoding="utf-8")
    current = observer.capture(workspace)

    assert baseline.is_git and not baseline.dirty
    assert current.dirty
    assert {"service.py", "untracked.txt"}.issubset(current.related_paths)
    assert "-value = 1" in current.patch
    assert "+value = 2" in current.patch
    assert "secret file contents" not in current.patch


def test_application_archives_model_reason_and_host_observed_diff(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    _initialize_repository(workspace)
    state = tmp_path / "state"
    runtime = EditingRuntime()
    app = build_application(settings=_settings(state), runtime=runtime)

    pending = app.start(workspace, "Update service.py to return value 2.")
    completed = app.approve(pending.session_id)
    session = app.get_session(pending.session_id)
    explanation = app.explain_change(pending.session_id, "service.py")

    types = [artifact.type for artifact in session.artifacts]
    assert completed.status == AgentStatus.COMPLETED
    assert ArtifactType.PROPOSAL in types
    assert ArtifactType.BASELINE_STATUS in types
    assert ArtifactType.BASELINE_PATCH in types
    assert ArtifactType.CHANGES_PATCH in types
    assert ArtifactType.TEST_RESULT in types
    assert ArtifactType.FINAL_REPORT in types
    baseline = next(item for item in session.artifacts if item.type == ArtifactType.BASELINE_STATUS)
    changes = next(item for item in session.artifacts if item.type == ArtifactType.CHANGES_PATCH)
    test_result = next(item for item in session.artifacts if item.type == ArtifactType.TEST_RESULT)
    assert baseline.metadata["dirty"] is False
    assert all(item.metadata["cycle_number"] == 1 for item in session.artifacts)
    assert changes.host_verified is True
    assert changes.metadata["baseline_was_dirty"] is False
    assert test_result.host_verified is False
    assert explanation.decisions
    assert changes.id in explanation.artifact_ids
    assert changes.event_id in explanation.event_ids
    assert any(record.reason for record in explanation.decisions)
    assert any(event.type == EventType.CODE_MODIFIED for event in session.events)
    assert any(event.type == EventType.TEST_EXECUTED for event in session.events)
    assert [run.status for run in session.runs] == [
        RunStatus.COMPLETED,
        RunStatus.COMPLETED,
    ]
    assert "value = 2" in Path(TaskArtifactStore(state).resolve_path(changes)).read_text(
        encoding="utf-8"
    )
