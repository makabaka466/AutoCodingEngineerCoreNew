"""任务理解随真实任务存储恢复，且不能冒充入口证据或扩大修改权限。"""

import pytest

from autocoding_agent.adapters.claude_code import ClaudeCodeSearchPolicyError
from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.engine import AgentEngine
from autocoding_agent.core.models import AgentDecision, AgentSession, AgentUsage, RuntimeResult
from autocoding_agent.core.search_policy import SearchPolicyViolation
from autocoding_agent.core.workflow import (
    TaskAnalysis,
    WorkflowAssessment,
    bind_assessment,
    task_analysis_context,
)
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import IncidentSession
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


def analysis():
    return TaskAnalysis(
        goal="核对入库扫码校验", expected="仅目标扫码事件阻止重复入库",
        scope=["Page.cs: txtUPN"], constraints=["不改其他页面接口"],
        acceptance=["目标事件重复扫码时提示，其他事件行为保持不变"],
        observations=[
            {"source": "image", "summary": "截图显示两个扫码框", "reference": "用户图片"},
            {"source": "user", "summary": "只修改 txtUPN"},
        ],
        investigation=[{"question": "txtUPN 写入前在哪校验？", "scope": "Page.cs 对应事件",
                        "done_when": "确认校验到写入的调用链", "status": "active"}],
    )


class AnalysisRuntime:
    def __init__(self):
        self.turns = []

    def run_observed(self, turn, run_id, event_sink):
        result = self.run_structured_observed(turn, AgentDecision, run_id, event_sink)
        return RuntimeResult(decision=result.output, runtime_session_id=turn.session_id)

    def run_structured_observed(self, turn, response_model, run_id, event_sink):
        self.turns.append(turn)
        if len(self.turns) == 2:
            raise ClaudeCodeSearchPolicyError(SearchPolicyViolation(
                tool_name="Grep", reason="搜索预算耗尽", budget_exhausted=True,
            ))
        return StructuredRuntimeResult(
            output=response_model(
                status="needs_input", message="已明确入口；请确认重复扫码是否允许？",
                question="重复扫码是否允许？",
                assessment=WorkflowAssessment(
                    phase="analyzing_request", reason="等待业务规则确认",
                    analysis=analysis() if len(self.turns) == 1 else None,
                    missing=["重复扫码规则"],
                ),
            ), runtime_session_id=turn.session_id, usage=AgentUsage(),
        )


@pytest.mark.parametrize("workflow", ["development", "incident"])
def test_analysis_survives_pause_restart_and_no_extra_planning_call(tmp_path, workflow):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = Settings(data_dir=tmp_path / "state", claude_command="unused.exe")
    build = build_application if workflow == "development" else build_incident_application
    runtime = AnalysisRuntime()
    app = build(settings=settings, runtime=runtime)
    first = app.start(workspace, "Page.cs 中只改 txtUPN，按图片分析")
    assert len(runtime.turns) == 1
    assert app.get_session(first.session_id).task_analysis == analysis()
    paused = app.send(first.session_id, "重复扫码不允许")
    assert paused.task_state.value == "paused"
    app = build(settings=settings, runtime=runtime)
    assert app.get_session(first.session_id).task_analysis == analysis()
    app.resume(first.session_id)
    assert len(runtime.turns) == 3
    assert "txtUPN" in runtime.turns[-1].system_prompt
    saved = app.get_session(first.session_id)
    assert saved.task_analysis == analysis()  # 省略不清空；暂停决策也不覆盖快照。
    assert saved.investigation_entry is None  # 任务分析不能冒充入口已核验。
    if workflow == "development":
        assert saved.pending_approval is None


@pytest.mark.parametrize("workflow", ["development", "incident"])
def test_new_cycle_archives_analysis_without_inheriting_scope(tmp_path, workflow):
    from types import SimpleNamespace

    cls = AgentSession if workflow == "development" else IncidentSession
    engine = AgentEngine if workflow == "development" else IncidentEngine
    session = cls(workspace=str(tmp_path), goal="旧需求", problem="旧异常")
    assert session.task_analysis is None  # 兼容旧快照。
    session.task_analysis = analysis()
    engine._reopen_completed_cycle(
        session, "另一个页面同样处理", SimpleNamespace(actor="user", id="x"),
    )
    assert session.task_analysis is None
    restored = cls.model_validate_json(session.model_dump_json())
    context = task_analysis_context(restored)
    assert "Historical only" in context and "txtUPN" in context
    assert restored.cycle_objective == "另一个页面同样处理"


def test_analysis_source_labels_do_not_verify_forged_code(tmp_path):
    session = AgentSession(workspace=str(tmp_path), goal="分析")
    supplied = analysis()
    supplied.observations[0].source = "code"
    decision = AgentDecision(status="completed", message="调查结束", assessment=WorkflowAssessment(
        phase="analyzing_request", reason="模型推测", analysis=supplied,
        evidence=[{"kind": "code", "reference": "Page.cs", "verified": True}],
    ))
    checked = bind_assessment(session, decision, mode="inspect")
    assert not checked.evidence[0].verified
    assert "任务理解" in supplied.display_text(compact=True)
    assert "收束条件" in supplied.display_text()


def test_analysis_has_bounded_payload():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskAnalysis(goal="x" * 241)
    with pytest.raises(ValidationError):
        TaskAnalysis(goal="分析", scope=["file"] * 6)

    current = analysis()
    pending = current.investigation[0].model_copy(
        update={"question": "下一步", "status": "pending"},
    )
    current.investigation.insert(0, pending)
    assert "txtUPN 写入前在哪校验" in current.display_text(compact=True)
    assert "下一步" not in current.display_text(compact=True)
