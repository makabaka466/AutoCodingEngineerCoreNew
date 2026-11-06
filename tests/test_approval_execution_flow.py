"""审批版本、文件变化与实施恢复的真实应用流程回归。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentMode,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    EventType,
    ProposedChange,
    RuntimeResult,
)
from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind, RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import StaleTaskVersion
from autocoding_agent.core.state_machine.models import TaskState


class EditingRuntime:
    """通过真实文件和宿主工具事件模拟实施，不将changed_files当作写入证据。"""

    def __init__(self, *, outside=False, interrupt=False, verify=False):
        self.turns = []
        self.outside = outside
        self.interrupt = interrupt
        self.verify = verify

    def run_observed(self, turn, run_id, event_sink):
        self.turns.append(turn)
        if turn.mode == AgentMode.INSPECT:
            decision = AgentDecision(
                status=AgentStatus.APPROVAL_REQUIRED, message="请审阅数值修复。",
                approval=ApprovalRequest(
                    scope=ApprovalScope.MODIFY, reason="修复数值。",
                    proposal=ChangeProposal(
                        summary="修复service数值。", expected_result="使用新数值。",
                        changes=[ProposedChange(
                            path="service.py", area="数值", current="value=1", proposed="value=2",
                        )],
                    ),
                ),
            )
        else:
            path = "outside.py" if self.outside else "service.py"
            if self.outside:
                # 模拟异步stdout事件晚于真实副作用，不能把停止等同于写入未发生。
                Path(turn.workspace, path).write_text("value=2\n", encoding="utf-8")
            event_sink(RuntimeActivity(
                run_id=run_id, kind=RuntimeEventKind.TOOL_STARTED, summary="写文件",
                tool_name="Edit", tool_use_id="write-1", data={"path": path},
            ))
            Path(turn.workspace, path).write_text("value=2\n", encoding="utf-8")
            if self.interrupt:
                raise RuntimeError("执行连接中断，写结果尚未返回")
            event_sink(RuntimeActivity(
                run_id=run_id, kind=RuntimeEventKind.TOOL_FINISHED, summary="已返回",
                tool_name="Edit", tool_use_id="write-1", data={"path": path, "is_error": False},
            ))
            decision = AgentDecision(
                status=AgentStatus.COMPLETED, message="修改完成，功能待验证。",
                changed_files=[path],
            )
            if self.verify:
                decision = AgentDecision(
                    status=AgentStatus.APPROVAL_REQUIRED, message="请批准验证。",
                    approval=ApprovalRequest(
                        scope=ApprovalScope.VERIFY, reason="运行现有测试。",
                        proposed_actions=["pytest tests/test_service.py"],
                    ), changed_files=[path],
                )
        return RuntimeResult(decision=decision, runtime_session_id="stage8-runtime")


def setup_task(tmp_path, runtime):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "service.py").write_text("value=1\n", encoding="utf-8")
    settings = Settings(
        claude_command="test-claude", claude_model="test-model", data_dir=tmp_path / "state",
    )
    app = build_application(settings=settings, runtime=runtime)
    outcome = app.start(workspace, "将数值调整到2，保持现有接口。")
    return app, settings, workspace, outcome


def test_restart_keeps_reviewed_snapshot_and_approval_is_idempotent(tmp_path):
    runtime = EditingRuntime()
    app, settings, workspace, pending = setup_task(tmp_path, runtime)
    snapshot = app.get_session(pending.session_id).pending_approval_snapshot
    restored = build_application(settings=settings, runtime=runtime)
    result = restored.approve(
        pending.session_id, command_id="once", expected_version=pending.version,
    )
    assert result.status == AgentStatus.COMPLETED
    session = restored.get_session(pending.session_id)
    assert session.implementation_report.changed_paths == ("service.py",)
    assert session.implementation_report.snapshot_id == snapshot.id
    assert "执行核对" in result.message
    assert (workspace / "service.py").read_text() == "value=2\n"
    restored.approve(pending.session_id, command_id="once", expected_version=pending.version)
    assert len(runtime.turns) == 2


@pytest.mark.parametrize("drift", ["file", "proposal", "legacy"])
def test_stale_approval_pauses_without_starting_write_runtime(tmp_path, drift):
    runtime = EditingRuntime()
    app, _, workspace, pending = setup_task(tmp_path, runtime)
    session = app.get_session(pending.session_id)
    if drift == "file":
        (workspace / "service.py").write_text("external=3\n", encoding="utf-8")
    elif drift == "proposal":
        session.pending_approval.proposal.changes[0].proposed = "value=99"
        app._engine.sessions.save(session)
    else:
        session.pending_approval_snapshot = None
        app._engine.sessions.save(session)
    result = app.approve(pending.session_id)
    assert result.task_state == TaskState.PAUSED
    assert result.approval is None
    assert "点击继续任务" in result.message
    assert len(runtime.turns) == 1


def test_old_displayed_version_cannot_approve_new_plan(tmp_path):
    runtime = EditingRuntime()
    app, _, _, pending = setup_task(tmp_path, runtime)
    app.send(pending.session_id, "请修订方案后重新展示。")
    with pytest.raises(StaleTaskVersion, match="方案已更新"):
        app.approve(pending.session_id, expected_version=pending.version)
    assert all(turn.mode == AgentMode.INSPECT for turn in runtime.turns)


@pytest.mark.parametrize("interrupted", [False, True])
def test_deviation_or_unknown_write_requires_read_only_recovery(tmp_path, interrupted):
    runtime = EditingRuntime(outside=not interrupted, interrupt=interrupted)
    app, settings, workspace, pending = setup_task(tmp_path, runtime)
    result = app.approve(pending.session_id)
    assert result.task_state == TaskState.RECOVERY_REQUIRED
    report = app.get_session(pending.session_id).implementation_report
    assert report.unverified_paths if interrupted else report.out_of_scope_paths
    restored = build_application(settings=settings, runtime=runtime)
    assert len(runtime.turns) == 2  # 启动恢复不能自动重放。
    restored.resume(pending.session_id)
    assert runtime.turns[-1].mode == AgentMode.INSPECT
    assert "最近实施核对" in runtime.turns[-1].user_message
    assert (workspace / ("service.py" if interrupted else "outside.py")).read_text() == "value=2\n"


def test_verification_approval_rechecks_just_implemented_files(tmp_path):
    runtime = EditingRuntime(verify=True)
    app, _, workspace, pending = setup_task(tmp_path, runtime)
    result = app.approve(pending.session_id)
    assert result.approval.scope == ApprovalScope.VERIFY
    snapshot = app.get_session(pending.session_id).pending_approval_snapshot
    assert not snapshot.issues
    assert any(item.path == "service.py" for item in snapshot.files)
    (workspace / "service.py").write_text("external=4\n", encoding="utf-8")
    result = app.approve(pending.session_id)
    assert result.task_state == TaskState.PAUSED
    assert len(runtime.turns) == 2


def test_power_loss_before_audit_rebuilds_file_report_without_runtime(tmp_path):
    runtime = EditingRuntime()
    app, settings, workspace, pending = setup_task(tmp_path, runtime)
    session = app.get_session(pending.session_id)
    session.approved_execution = session.pending_approval_snapshot
    session.pending_approval = None
    session.pending_approval_snapshot = None
    app._engine.state_machine.transition(
        session, TaskState.IMPLEMENTING, reason="已批准，模拟进程断电。",
    )
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    run = RuntimeRunRecord(
        task_id=session.id, state=TaskState.IMPLEMENTING, mode="implement",
        owner_id="dead-process", owner_pid=99_999_999, started_at=old, heartbeat_at=old,
    )
    session.runs.append(run)
    session.events.extend([
        AgentEvent(type=EventType.RUNTIME_STARTED, message="started", command_id="power-loss",
                   correlation_id=run.id),
        AgentEvent(type=EventType.TOOL_STARTED, message="edit", command_id="power-loss",
                   correlation_id=run.id,
                   data={"tool_name": "Edit", "tool_use_id": "w", "path": "service.py"}),
    ])
    app._engine.sessions.save(session)
    (workspace / "service.py").write_text("value=2\n", encoding="utf-8")
    restored = build_application(settings=settings, runtime=runtime).get_session(session.id)
    assert restored.task_state == TaskState.RECOVERY_REQUIRED
    assert restored.implementation_report.changed_paths == ("service.py",)
    assert restored.implementation_report.unverified_paths == ("service.py",)
    assert restored.implementation_report.interrupted
    assert len(runtime.turns) == 1
