"""审批绑定与执行文件核对：不调用模型，不依赖 Git 或 SVN 安装。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from autocoding_agent.core import approval_execution
from autocoding_agent.core.approval_execution import (
    approval_changes,
    approval_file_changes,
    audit_implementation,
    report_text,
    snapshot_approval,
)
from autocoding_agent.core.approval_execution_models import ApprovalSnapshot
from autocoding_agent.core.models import (
    AgentEvent,
    AgentSession,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    ChatMessage,
    EventType,
    MessageRole,
    ProposedChange,
)
from autocoding_agent.core.workflow import read_fingerprint


def _approval(path: str | None = "source.py") -> ApprovalRequest:
    return ApprovalRequest(
        scope=ApprovalScope.MODIFY,
        reason="修正校验逻辑",
        proposed_actions=["编辑明确文件"],
        proposal=ChangeProposal(
            summary="修正校验逻辑",
            changes=[ProposedChange(path=path, area="校验", current="旧值", proposed="新值")],
            expected_result="正确校验",
        ),
    )


def _event(
    session: AgentSession, path: str, *, tool: str = "Read", finished: bool = True,
    command: str = "inspect", operation: str = "tool-1", is_error: bool = False,
) -> AgentEvent:
    return AgentEvent(
        type=EventType.TOOL_FINISHED if finished else EventType.TOOL_STARTED,
        message="工具记录", command_id=command, correlation_id="run-1",
        data={"tool_name": tool, "path": path, "is_error": is_error,
              "tool_use_id": operation,
              "content_sha256": read_fingerprint(session.workspace, path)},
    )


def test_snapshot_is_immutable_and_persists_exact_approval(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    approval = _approval()
    snapshot = snapshot_approval(session, approval)
    assert not snapshot.issues
    assert snapshot.files[0].state == "missing"
    assert ApprovalSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    with pytest.raises(ValidationError):
        snapshot.approval_json = "changed"
    assert approval_changes(session, approval, snapshot) == []
    approval.proposal.impact.append("影响另一入口")
    assert "方案内容已变化" in "".join(approval_changes(session, approval, snapshot))


@pytest.mark.parametrize("marker", [None, ".git", ".svn"])
def test_fingerprints_do_not_depend_on_repository_kind(tmp_path: Path, marker: str | None) -> None:
    if marker:
        (tmp_path / marker).mkdir()
    target = tmp_path / "source.py"
    target.write_text("old", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    approval = _approval()
    snapshot = snapshot_approval(session, approval)
    target.write_text("new", encoding="utf-8")
    assert approval_changes(session, approval, snapshot)
    report = audit_implementation(session, snapshot, "implement")
    assert report.changed_paths == ("source.py",)
    assert not report.unverified_paths


def test_stale_read_dependency_prevents_approval(tmp_path: Path) -> None:
    (tmp_path / "dependency.py").write_text("old", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    session.events.append(_event(session, "dependency.py"))
    (tmp_path / "dependency.py").write_text("changed", encoding="utf-8")
    snapshot = snapshot_approval(session, _approval())
    assert any("读取依据" in reason for reason in snapshot.issues)
    session.events.append(_event(session, "dependency.py", operation="read-again"))
    assert not snapshot_approval(session, _approval()).issues


def test_changed_goal_and_user_input_invalidate_approval(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    approval = _approval()
    snapshot = snapshot_approval(session, approval)
    session.messages.append(ChatMessage(role=MessageRole.USER, content="新增要求"))
    assert "任务需求" in "".join(approval_changes(session, approval, snapshot))
    # 批准后追加宿主授权消息，首次实施前的第二次文件核对仍可使用。
    assert approval_file_changes(session, snapshot) == []


def test_legacy_missing_snapshot_requires_reapproval(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    assert "历史审批" in "".join(approval_changes(session, _approval(), None))


def test_successful_edit_refreshes_earlier_read_for_next_approval(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text("before", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    session.events.append(_event(session, "source.py"))
    target.write_text("after", encoding="utf-8")
    session.events.append(_event(session, "source.py", tool="Edit", command="implement"))
    assert not snapshot_approval(session, _approval()).issues
    verify = ApprovalRequest(scope=ApprovalScope.VERIFY, reason="执行验证", proposal=None)
    assert not snapshot_approval(session, verify).issues
    target.write_text("external change", encoding="utf-8")
    assert snapshot_approval(session, verify).issues


def test_verification_inherits_files_without_observable_runtime_events(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    session.approved_execution = snapshot_approval(session, _approval())
    (tmp_path / "source.py").write_text("implemented", encoding="utf-8")
    verify = ApprovalRequest(scope=ApprovalScope.VERIFY, reason="执行验证", proposal=None)
    snapshot = snapshot_approval(session, verify)
    assert not snapshot.issues
    assert snapshot.planned_paths == ()
    assert snapshot.files[0].path == "source.py"
    assert snapshot.files[0].state == "present"
    (tmp_path / "source.py").write_text("external change", encoding="utf-8")
    assert approval_changes(session, verify, snapshot)


@pytest.mark.parametrize("path", [None, "../outside.py", "*.py", "."])
def test_ambiguous_or_outside_paths_are_not_write_scope(tmp_path: Path, path: str | None) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    snapshot = snapshot_approval(session, _approval(path))
    assert snapshot.issues
    assert snapshot.planned_paths == ()


def test_large_files_are_explicitly_unverified(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    snapshot = snapshot_approval(session, _approval())
    assert snapshot.files[0].state == "unverified"
    assert snapshot.issues


def test_snapshot_budget_is_bounded_without_directory_scan(tmp_path: Path, monkeypatch) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    approval = _approval()
    approval.proposal.changes.extend([
        ProposedChange(path=f"extra-{index}.py", area="校验", current="旧值", proposed="新值")
        for index in range(3)
    ])
    monkeypatch.setattr(approval_execution, "MAX_SNAPSHOT_FILES", 2)

    def deny_scan(*args, **kwargs):
        raise AssertionError("审批核对不能递归扫描目录")

    monkeypatch.setattr(Path, "rglob", deny_scan)
    monkeypatch.setattr(Path, "iterdir", deny_scan)
    snapshot = snapshot_approval(session, approval)
    assert len(snapshot.files) == 2
    assert any("超过 2 个" in issue for issue in snapshot.issues)


def test_new_file_creation_after_review_invalidates_approval(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="创建文件")
    approval = _approval()
    snapshot = snapshot_approval(session, approval)
    assert snapshot.files[0].state == "missing"
    (tmp_path / "source.py").write_text("user work", encoding="utf-8")
    assert approval_changes(session, approval, snapshot)


def test_model_claim_does_not_prove_file_changes(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    snapshot = snapshot_approval(session, _approval())
    report = audit_implementation(session, snapshot, "implement", ["source.py", "other.py"])
    assert report.changed_paths == ()
    assert report.unchanged_paths == ("source.py",)
    assert report.reported_only_paths == ("other.py", "source.py")
    assert "功能验证通过" in report_text(report)


def test_incomplete_write_is_preserved_and_unrelated_commands_are_ignored(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    snapshot = snapshot_approval(session, _approval())
    session.events.extend([
        _event(session, "source.py", tool="Write", finished=False, command="implement"),
        _event(session, "old.py", tool="Write", command="previous"),
    ])
    (tmp_path / "source.py").write_text("new", encoding="utf-8")
    report = audit_implementation(session, snapshot, "implement", interrupted=True)
    assert report.changed_paths == ("source.py",)
    assert report.unverified_paths == ("source.py",)
    assert not report.out_of_scope_paths
    assert report.interrupted


def test_observed_outside_write_and_dependency_drift_are_visible(tmp_path: Path) -> None:
    (tmp_path / "dependency.py").write_text("before", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    session.events.append(_event(session, "dependency.py"))
    snapshot = snapshot_approval(session, _approval())
    (tmp_path / "dependency.py").write_text("outside drift", encoding="utf-8")
    session.events.append(_event(session, "other.py", tool="Write", command="implement"))
    report = audit_implementation(session, snapshot, "implement")
    assert report.out_of_scope_paths == ("dependency.py", "other.py")


def test_repeated_writes_use_last_completed_fingerprint(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    snapshot = snapshot_approval(session, _approval())
    for number in (1, 2):
        (tmp_path / "source.py").write_text(str(number), encoding="utf-8")
        session.events.append(_event(
            session, "source.py", tool="Write", command="implement", operation=f"write-{number}"))
    report = audit_implementation(session, snapshot, "implement")
    assert report.changed_paths == ("source.py",)
    assert not report.unverified_paths
    (tmp_path / "source.py").write_text("external modification", encoding="utf-8")
    assert audit_implementation(session, snapshot, "implement").unverified_paths == ("source.py",)


def test_new_cycle_does_not_reuse_previous_read_evidence(tmp_path: Path) -> None:
    (tmp_path / "old.py").write_text("old", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="修改校验")
    session.events.extend([
        _event(session, "old.py"),
        AgentEvent(type=EventType.TASK_REOPENED, message="新的需求"),
    ])
    (tmp_path / "old.py").write_text("changed", encoding="utf-8")
    snapshot = snapshot_approval(session, _approval())
    assert not snapshot.issues
    assert tuple(item.path for item in snapshot.files) == ("source.py",)
