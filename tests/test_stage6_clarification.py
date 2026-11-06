"""澄清门槛覆盖真实持久化流程，不能靠隐藏按钮绕过。"""

import pytest
from test_agent_flows import ScriptedRuntime, _approval, _settings
from test_incident_flow import ScriptedStructuredRuntime, _page
from test_workflow_assessment import _assessment, _incident, _read_event

from autocoding_agent.application import build_application
from autocoding_agent.core.models import AgentSession, ApprovalScope
from autocoding_agent.core.progress import ProgressPhase
from autocoding_agent.core.workflow import ResultKind, WorkflowAssessment, bind_assessment
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import IncidentDecision


def test_questions_block_proposal_until_answer_after_restart(tmp_path):
    pending = _approval(ApprovalScope.MODIFY)
    pending.assessment = WorkflowAssessment(
        phase=ProgressPhase.WAITING_APPROVAL, reason="确认清空范围。",
        user_questions=["清空会影响历史绑定：只清界面，还是删除已保存记录？"],
    )
    ready = _approval(ApprovalScope.MODIFY)
    ready.assessment = WorkflowAssessment(
        phase=ProgressPhase.WAITING_APPROVAL, reason="用户已选择只清界面。",
        verification_pending=["实施后编译并检查清空行为。"],
    )
    runtime = ScriptedRuntime(pending, ready)
    settings = _settings(tmp_path / "state")
    app = build_application(settings=settings, runtime=runtime)
    outcome = app.start(tmp_path, "给页面增加清空按钮")
    assert outcome.task_state.value == "waiting_input"
    assert "只清界面" in outcome.message
    assert app.get_session(outcome.session_id).pending_approval is None
    with pytest.raises(ValueError, match="no pending approval"):
        app.approve(outcome.session_id)
    assert len(runtime.turns) == 1
    app = build_application(settings=settings, runtime=runtime)
    outcome = app.send(outcome.session_id, "只清界面，不删除记录")
    assert outcome.task_state.value == "waiting_modify_approval"
    assert len(runtime.turns) == 2  # 不增加固定的澄清或规划模型调用。


def test_incident_with_unresolved_choice_cannot_complete_or_remediate(tmp_path):
    assessment = WorkflowAssessment(
        phase=ProgressPhase.DIAGNOSING_CAUSE, reason="需要确认现场规则。",
        user_questions=["该工序是否允许重复绑定？允许或不允许会改变处理方案。"],
    )
    decision = IncidentDecision(status="completed", message="草案", diagnosis="尚有业务歧义",
                                page=_page(), recommended_actions=["修改校验"],
                                assessment=assessment)
    app = build_incident_application(settings=_settings(tmp_path / "state"),
                                    runtime=ScriptedStructuredRuntime([decision]))
    result = app.start(tmp_path, "调查绑定问题")
    assert result.task_state.value == "waiting_input"
    assert "是否允许重复绑定" in result.message
    assert "修改校验" not in result.message
    assert not app.get_session(result.session_id).can_remediate


def test_persisted_approval_with_questions_cannot_bypass_gate(tmp_path):
    app = build_application(settings=_settings(tmp_path / "state"),
                            runtime=ScriptedRuntime(_approval(ApprovalScope.MODIFY)))
    outcome = app.start(tmp_path, "修改页面")
    saved = app.get_session(outcome.session_id)
    saved.last_decision.assessment.user_questions = ["是否删除历史绑定？"]
    app._engine.sessions.save(saved)
    with pytest.raises(ValueError, match="仍有未澄清"):
        app.approve(outcome.session_id)
    assert len(app._engine.runtime.turns) == 1


def test_causal_chain_is_required_but_does_not_replace_read_evidence(tmp_path):
    (tmp_path / "page.py").write_text("raise ValueError('invalid')", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="调查异常")
    session.events.append(_read_event(session))
    decision = _incident(_assessment(ResultKind.CAUSE_CONFIRMED))
    decision.assessment.causal_chain = []
    assert bind_assessment(session, decision, mode="inspect").result == ResultKind.HYPOTHESIS
    decision.assessment.causal_chain = ["输入触发校验", "校验抛出错误"]
    assert bind_assessment(session, decision, mode="inspect").result == ResultKind.CAUSE_CONFIRMED
    session.events.clear()
    assert bind_assessment(session, decision, mode="inspect").result == ResultKind.HYPOTHESIS
