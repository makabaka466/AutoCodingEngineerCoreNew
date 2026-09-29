"""验证证据与结果等级的边界，不调用真实模型或业务数据库。"""

from pathlib import Path

import pytest

from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentSession,
    AgentStatus,
    EventType,
)
from autocoding_agent.core.progress import ProgressPhase, ProgressWorkflow
from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind
from autocoding_agent.core.runtime_lifecycle import RuntimeLifecycle
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.core.workflow import (
    ResultKind,
    WorkflowAssessment,
    WorkflowEvidence,
    bind_assessment,
    evidence_catalog,
    read_fingerprint,
)
from autocoding_agent.incident.models import (
    IncidentDecision,
    IncidentStatus,
    LocatedPage,
)


def _assessment(result: ResultKind, *, kind: str = "code", reference: str = "page.py"):
    return WorkflowAssessment(
        phase=ProgressPhase.DIAGNOSING_CAUSE,
        reason="当前源码解释异常分支，继续按证据判断。",
        confirmed=["已读取页面源码。"],
        evidence=[WorkflowEvidence(kind=kind, reference=reference)],
        result=result,
    )


def _incident(assessment: WorkflowAssessment) -> IncidentDecision:
    return IncidentDecision(
        status=IncidentStatus.COMPLETED,
        message="原因已找到。",
        page=LocatedPage(
            name="页面",
            source_paths=["page.py"],
            explanation="入口一致。",
            matched_evidence=["页面标题一致。"],
        ),
        diagnosis="代码中的分支解释了该现象。",
        assessment=assessment,
    )


def _read_event(session: AgentSession) -> AgentEvent:
    return AgentEvent(
        type=EventType.TOOL_FINISHED,
        message="Read completed.",
        data={
            "tool_name": "Read",
            "path": "page.py",
            "is_error": False,
            "content_sha256": read_fingerprint(session.workspace, "page.py"),
        },
    )


def test_cause_confirmation_requires_actual_current_read_and_no_gaps(tmp_path: Path) -> None:
    (tmp_path / "page.py").write_text("raise ValueError('invalid')", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="调查异常")
    event = _read_event(session)
    session.events.append(event)
    decision = _incident(_assessment(ResultKind.CAUSE_CONFIRMED))
    assessment = bind_assessment(session, decision, mode="inspect")
    assert assessment.result == ResultKind.CAUSE_CONFIRMED
    assert assessment.evidence[0].event_id == event.id
    assert assessment.evidence[0].verified

    decision.assessment.missing = ["尚未确认部署版本。"]
    assert bind_assessment(session, decision, mode="inspect").result == ResultKind.HYPOTHESIS

    decision.assessment.missing = []
    (tmp_path / "page.py").write_text("pass", encoding="utf-8")
    stale = bind_assessment(session, decision, mode="inspect")
    assert stale.result == ResultKind.HYPOTHESIS
    assert not stale.evidence[0].verified
    assert stale.missing


@pytest.mark.parametrize("is_error", [True, False])
def test_model_cannot_forge_verified_source_or_bind_previous_cycle(
    tmp_path: Path,
    is_error: bool,
) -> None:
    (tmp_path / "page.py").write_text("pass", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="调查异常")
    read = _read_event(session)
    read.data["is_error"] = is_error
    session.events = [read, AgentEvent(type=EventType.TASK_REOPENED, message="New cycle.")]
    decision = _incident(_assessment(ResultKind.CAUSE_CONFIRMED))
    decision.assessment.evidence[0].verified = True
    decision.assessment.evidence[0].event_id = "forged-event"
    assessment = bind_assessment(session, decision, mode="inspect")
    assert assessment.result == ResultKind.HYPOTHESIS
    assert not assessment.evidence[0].verified
    assert assessment.evidence[0].event_id is None


def test_query_reference_binds_successful_audit_without_business_values(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="调查异常")
    event = AgentEvent(
        type=EventType.DATABASE_QUERIES_EXECUTED,
        message="Read-only query completed.",
        data={"queries": [{"name": "order_state", "sql_fingerprint": "hash"}]},
    )
    session.events = [event]
    decision = _incident(
        _assessment(ResultKind.CAUSE_CONFIRMED, kind="query", reference="order_state")
    )
    assessment = bind_assessment(session, decision, mode="inspect")
    assert assessment.evidence[0].verified
    assert assessment.evidence[0].event_id == event.id
    # 数据样本本身不能替代源码因果证据。
    assert assessment.result == ResultKind.HYPOTHESIS
    assert "order_state" in evidence_catalog(session)
    assert "sql_fingerprint" not in evidence_catalog(session)


@pytest.mark.parametrize(
    "exit_code,is_error,expected",
    [
        (0, False, ResultKind.VERIFIED),
        (1, False, ResultKind.MODIFIED_UNVERIFIED),
        (None, False, ResultKind.MODIFIED_UNVERIFIED),
        (0, True, ResultKind.MODIFIED_UNVERIFIED),
    ],
)
def test_verification_needs_successful_observed_command(
    tmp_path: Path,
    exit_code: int | None,
    is_error: bool,
    expected: ResultKind,
) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修复")
    session.events = [
        AgentEvent(
            type=EventType.TOOL_FINISHED,
            message="Command returned.",
            data={
                "tool_name": "Bash",
                "tool_use_id": "test-1",
                "is_error": is_error,
                "exit_code": exit_code,
            },
        )
    ]
    decision = AgentDecision(
        status=AgentStatus.COMPLETED,
        message="完成。",
        assessment=_assessment(ResultKind.VERIFIED, kind="validation", reference="test-1"),
    )
    assert bind_assessment(session, decision, mode="verify").result == expected
    assert bind_assessment(session, decision, mode="implement").result == (
        ResultKind.MODIFIED_UNVERIFIED
    )


def test_new_edit_stage_invalidates_earlier_verification(tmp_path: Path) -> None:
    session = AgentSession(workspace=str(tmp_path), goal="修复")
    session.events = [
        AgentEvent(type=EventType.TOOL_FINISHED, message="Earlier test succeeded.", data={
            "tool_name": "Bash", "tool_use_id": "old-test", "is_error": False, "exit_code": 0,
        }),
        AgentEvent(type=EventType.STATE_TRANSITIONED, message="New edit stage.", data={
            "to": "implementing",
        }),
    ]
    decision = AgentDecision(
        status=AgentStatus.COMPLETED, message="完成。",
        assessment=_assessment(ResultKind.VERIFIED, kind="validation", reference="old-test"),
    )
    assessment = bind_assessment(session, decision, mode="verify")
    assert assessment.result == ResultKind.MODIFIED_UNVERIFIED
    assert not assessment.evidence[0].verified


def test_runtime_records_read_hash_without_source_body(tmp_path: Path) -> None:
    (tmp_path / "page.py").write_text("private_source_body", encoding="utf-8")
    session = AgentSession(workspace=str(tmp_path), goal="调查", task_state=TaskState.INSPECTING)
    lifecycle = RuntimeLifecycle(
        workflow=ProgressWorkflow.DEVELOPMENT,
        owner_id="test",
        save=lambda _: None,
    )
    run = lifecycle.start(session, mode="inspect", command_id="test-command")
    lifecycle.record_activity(
        session,
        run,
        RuntimeActivity(
            run_id=run.id,
            kind=RuntimeEventKind.TOOL_FINISHED,
            tool_name="Read",
            summary="Read completed.",
            data={"path": "page.py", "is_error": False},
        ),
        command_id="test-command",
        mode="inspect",
        progress_sink=None,
    )
    assert session.events[-1].data["content_sha256"]
    assert "private_source_body" not in session.model_dump_json()
    assert read_fingerprint(str(tmp_path), "../outside.py") is None


def test_legacy_decisions_load_without_claiming_verified_results(tmp_path: Path) -> None:
    decision = AgentDecision.model_validate({"status": "completed", "message": "旧结果。"})
    assert decision.assessment is None
    session = AgentSession(workspace=str(tmp_path), goal="旧任务")
    assert bind_assessment(session, decision, mode="implement").result == (
        ResultKind.MODIFIED_UNVERIFIED
    )
