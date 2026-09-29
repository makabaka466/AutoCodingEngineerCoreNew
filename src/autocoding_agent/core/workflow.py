"""模型解释推进条件，宿主关联实际证据；不替模型判断业务因果。"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from autocoding_agent.core.progress import ProgressPhase


class ResultKind(StrEnum):
    """轮次结束的交付含义，与任务生命周期分开。"""

    UNSPECIFIED = "unspecified"
    ANALYSIS = "analysis"
    PAGE_LOCATED = "page_located"
    HYPOTHESIS = "hypothesis"
    CAUSE_CONFIRMED = "cause_confirmed"
    MODIFIED_UNVERIFIED = "modified_unverified"
    VERIFIED = "verified"
    PARTIAL = "partial"


RESULT_LABELS = {
    ResultKind.UNSPECIFIED: "本轮结束，结果等级未声明",
    ResultKind.ANALYSIS: "只读分析完成",
    ResultKind.PAGE_LOCATED: "页面定位完成",
    ResultKind.HYPOTHESIS: "候选原因，尚待验证",
    ResultKind.CAUSE_CONFIRMED: "原因已确认",
    ResultKind.MODIFIED_UNVERIFIED: "修改完成，尚未验证",
    ResultKind.VERIFIED: "修改验证完成",
    ResultKind.PARTIAL: "调查暂止，证据尚不充分",
}


class WorkflowEvidence(BaseModel):
    """引用实际操作记录；verified 仅表示来源已核对，不表示因果已证明。"""

    kind: Literal["code", "query", "validation"] = Field(
        description="证据来源：已读源码、已执行查询或验证命令。"
    )
    reference: str = Field(min_length=1, description="源码相对路径、查询名称或工具调用 ID。")
    event_id: str | None = Field(default=None, description="宿主绑定的实际操作事件 ID。")
    verified: bool = Field(default=False, description="宿主是否找到本轮成功操作记录。")


class WorkflowAssessment(BaseModel):
    """当前阶段、推进理由及证据缺口；只返回简短结论，不返回思维链。"""

    phase: ProgressPhase = Field(description="当前业务阶段；执行工具时由实际工具事件更新 UI。")
    confirmed: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="模型已确认的关键事实摘要，不得包含原始业务数据。",
    )
    reason: str = Field(min_length=1, description="为何继续、回退、提问或结束的简短可核对依据。")
    missing: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="尚缺的关键证据或验证条件；为空不代表宿主认可因果。",
    )
    evidence: list[WorkflowEvidence] = Field(
        default_factory=list, max_length=12, description="关键结论引用的本轮源码、查询或验证记录。"
    )
    result: ResultKind = Field(
        default=ResultKind.UNSPECIFIED, description="本轮交付等级；completed 不自动表示问题已解决。"
    )


WORKFLOW_RULES = """## Advancement
Return assessment: phase, confirmed facts, public reason, missing evidence and references
(Read relative paths, host query names, validation IDs). Host fills event_id/verified; verified
source is not causal proof. No private reasoning, raw rows, SQL parameters or secrets.
Judge sufficiency: close a named gap per action, skip unnecessary steps, revisit contradictions,
ask only for facts tools cannot obtain, stop repeated actions without new evidence. Confidence
is not a gate. Completed ends a cycle, not necessarily the problem. Use hypothesis for unproven
causes; cause_confirmed needs cited source and no causal gaps; modified_unverified without
successful relevant validation; partial when exhausted. Normal tool return is not test success.
Diagnosis never applies remediation. Keep message certainty aligned with result.
"""


def read_fingerprint(workspace: str, reference: str) -> str | None:
    """只计算工作区内小型普通文件的指纹，不保存正文、不读取工作区外文件。"""
    root = Path(workspace).resolve()
    try:
        target = (root / reference).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        if target.stat().st_size > 4 * 1024 * 1024:
            return None
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None


def cycle_events(session: Any) -> list[Any]:
    """新轮次撤销旧证据自动背书，保留本轮澄清/审批前取得的证据。"""
    start = 0
    for index, event in enumerate(session.events):
        if event.type.value == "task_reopened":
            start = index + 1
    return session.events[start:]


def bind_assessment(session: Any, decision: Any, *, mode: str) -> WorkflowAssessment:
    """核对证据出处并保守规范化完成等级，语义充分性仍由模型判断。"""
    supplied = decision.assessment
    assessment = (
        supplied.model_copy(deep=True)
        if supplied
        else WorkflowAssessment(
            phase=ProgressPhase.ANALYZING_REQUEST,
            reason="模型未声明阶段推进依据；仅保留本轮结果，不自动确认问题已解决。",
        )
    )
    events = cycle_events(session)
    # 新的写阶段会使旧验证失效；同一轮内重新修改也必须重新验证。
    validation_start = 0
    for index, event in enumerate(events):
        if event.type.value == "state_transitioned" and event.data.get("to") == "implementing":
            validation_start = index + 1
    for item in assessment.evidence:
        item.event_id, item.verified = None, False
        eligible = events[validation_start:] if item.kind == "validation" else events
        for event in reversed(eligible):
            data = event.data
            if item.kind == "query":
                matched = event.type.value == "database_queries_executed" and any(
                    row.get("query_name", row.get("name")) == item.reference
                    and row.get("status", "succeeded") == "succeeded"
                    for row in data.get("queries", [])
                )
            elif item.kind == "code":
                matched = (
                    event.type.value == "tool_finished"
                    and str(data.get("tool_name", "")).casefold() == "read"
                    and not data.get("is_error", True)
                    and str(data.get("path", "")).replace("\\", "/").casefold()
                    == item.reference.replace("\\", "/").casefold()
                    and data.get("content_sha256") is not None
                    and data["content_sha256"]
                    == read_fingerprint(session.workspace, item.reference)
                )
            else:
                matched = (
                    event.type.value == "tool_finished"
                    and str(data.get("tool_name", "")).casefold() == "bash"
                    and data.get("tool_use_id") == item.reference
                    and not data.get("is_error", True)
                    and data.get("exit_code") == 0
                )
            if matched:
                item.event_id, item.verified = event.id, True
                break

    requested_result = assessment.result
    if decision.status.value == "completed":
        if hasattr(decision, "completion_kind"):
            if decision.completion_kind.value == "page_location":
                assessment.result = ResultKind.PAGE_LOCATED
            elif assessment.result != ResultKind.PARTIAL:
                if not (
                    assessment.result == ResultKind.CAUSE_CONFIRMED
                    and not assessment.missing
                    and any(item.kind == "code" and item.verified for item in assessment.evidence)
                    and all(item.verified for item in assessment.evidence)
                ):
                    assessment.result = ResultKind.HYPOTHESIS
        elif mode == "implement":
            assessment.result = ResultKind.MODIFIED_UNVERIFIED
        elif mode == "verify":
            if not (
                assessment.result == ResultKind.VERIFIED
                and not assessment.missing
                and any(item.kind == "validation" and item.verified for item in assessment.evidence)
                and all(item.verified for item in assessment.evidence)
            ):
                assessment.result = ResultKind.MODIFIED_UNVERIFIED
        elif assessment.result not in {ResultKind.PARTIAL, ResultKind.HYPOTHESIS}:
            assessment.result = ResultKind.ANALYSIS
        assessment.phase = ProgressPhase.COMPLETED
    elif decision.status.value == "needs_input":
        assessment.phase = ProgressPhase.WAITING_INPUT
    elif decision.status.value == "approval_required":
        assessment.phase = ProgressPhase.WAITING_APPROVAL
    elif decision.status.value == "query_required":
        stage = getattr(decision, "query_stage", None)
        assessment.phase = (
            ProgressPhase.LOCATING_PAGE
            if stage is not None and stage.value == "page_lookup"
            else ProgressPhase.QUERYING_DATABASE
        )
    elif decision.status.value == "failed":
        assessment.phase = ProgressPhase.FAILED
    elif decision.status.value == "hermes_skill_required":
        assessment.phase = ProgressPhase.CONSULTING_ENGINEERING_EXPERIENCE
    if any(not item.verified for item in assessment.evidence):
        gap = "部分证据引用未关联本轮成功操作，或文件已变化；相关结论仍需核验。"
        if gap not in assessment.missing and len(assessment.missing) < 6:
            assessment.missing.append(gap)
    if requested_result in {ResultKind.CAUSE_CONFIRMED, ResultKind.VERIFIED}:
        if assessment.result != requested_result and len(assessment.missing) < 6:
            assessment.missing.append("未满足已确认结果所需的本轮可核对证据和验证条件。")
    return assessment


def assessment_text(assessment: WorkflowAssessment | None) -> str:
    """桌面、Web 与能力文档共用的公开摘要。"""
    if assessment is None:
        return ""
    lines = ["推进依据：" + assessment.reason]
    if assessment.confirmed:
        lines.append("已确认：" + "；".join(assessment.confirmed))
    if assessment.missing:
        lines.append("尚缺证据：" + "；".join(assessment.missing))
    if assessment.result != ResultKind.UNSPECIFIED:
        lines.append("本轮结果：" + RESULT_LABELS[assessment.result])
    for item in assessment.evidence:
        lines.append(
            f"证据来源：{item.reference}（{'已关联操作记录' if item.verified else '待核验'}）"
        )
    return "\n".join(lines)


def assessment_progress_text(assessment: WorkflowAssessment | None) -> str:
    """状态区保持紧凑，完整事实和引用留在可滚动的对话中。"""
    if assessment is None:
        return ""
    reason = " ".join(assessment.reason.split())
    text = "推进依据：" + reason[:120] + ("…" if len(reason) > 120 else "")
    if assessment.missing:
        missing = "；".join(assessment.missing)
        text += "\n尚缺证据：" + missing[:120] + ("…" if len(missing) > 120 else "")
    return text


def evidence_catalog(session: Any) -> str:
    """提供有界引用目录，不把源码、SQL、参数或原始业务行复制进提示词。"""
    references = []
    for event in cycle_events(session):
        data = event.data
        if event.type.value == "tool_finished" and not data.get("is_error", True):
            tool = str(data.get("tool_name", "")).casefold()
            if tool == "read" and data.get("content_sha256"):
                references.append({"kind": "code", "reference": data.get("path")})
            elif tool == "bash":
                references.append(
                    {
                        "kind": "validation",
                        "reference": data.get("tool_use_id"),
                        "exit_code": data.get("exit_code"),
                    }
                )
        elif event.type.value == "database_queries_executed":
            references.extend(
                {"kind": "query", "reference": row.get("query_name", row.get("name"))}
                for row in data.get("queries", [])
            )
    if not references:
        return ""
    return (
        "\n<host_evidence_catalog>\n"
        + json.dumps(references[-12:], ensure_ascii=False)
        + "\n</host_evidence_catalog>\n"
    )
