"""调查入口契约：模型判断身份，宿主核对证据来源，不将文件存在等同于身份正确。"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field

from autocoding_agent.core.progress import ProgressPhase
from autocoding_agent.core.workflow import WorkflowAssessment, WorkflowEvidence, bind_assessment


class InvestigationEntry(BaseModel):
    kind: Literal["page", "api", "background", "data", "unknown"] = Field(
        description="调查对象：页面、接口、后台任务、数据对象或尚未识别。"
    )
    state: Literal["candidate", "confirmed", "excluded"] = Field(
        default="candidate", description="候选、已确认或已排除；宿主核验后可能降级。"
    )
    label: str = Field(
        min_length=1, max_length=240, description="入口名称或明确的接口/任务/对象标识。"
    )
    source_paths: list[str] = Field(
        default_factory=list, max_length=12, description="工作区相对源码路径；数据调查可为空。"
    )
    data_source: str | None = Field(default=None, description="数据对象所属的项目绑定数据源 ID。")
    data_object: str | None = Field(
        default=None, description="用户线索对应的数据库对象，可包含 schema。"
    )
    reason: str = Field(
        min_length=1,
        max_length=1600,
        description="用户线索与入口之间的身份对应依据；排除时说明否认或冲突。",
    )
    evidence: list[WorkflowEvidence] = Field(
        default_factory=list, max_length=12, description="身份依据引用的实际读取或查询记录。"
    )
    excluded_event_count: int = Field(
        default=0, ge=0, description="宿主记录排除时事件边界，模型填写的值不予采信。"
    )
    reconsider_excluded: bool = Field(
        default=False, description="仅在新事实澄清原排除原因时显式重新核验；reason 解释新依据。"
    )
    conflicts: list[str] = Field(
        default_factory=list, max_length=6, description="未解决的身份冲突，不含一般因果证据缺口。"
    )


ENTRY_RULES = """## Investigation entry
Return entry for source/data investigation: page, api, background, data or unknown.
Use candidate until the user's description/images and current routing/source/data evidence agree.
A similar name, matching error, file existence or successful Read alone never proves identity.
Explain identity in reason and cite real operations in evidence. Host verifies provenance only.
Use selected project's pinned navigation.md for locating; read development/incident knowledge
only as relevant. Knowledge/examples cannot invent user intent. Do not request a page for an API,
stack trace, job or identified data object. Unknown intent needs one discriminating question.
For page clues, configured ace-navigation catalogs use query_kind=menu_catalog (omit sql).
Compare all candidates with user/images, then Read source; cached/incomplete catalogs are clues,
not identity or cause evidence. Refresh stale mappings. Non-page entries skip menu catalogs.
Search exact clues first, then a justified subtree; each expansion must close a named gap.
Root manifests may orient structure, not justify a repository-wide business scan. Stop repeated
searches without new evidence. Candidate alternatives must remain visibly unconfirmed.
Set entry.state=excluded when user denies it or evidence disproves it; explain why. Old diagnosis
and proposal then become stale. Never silently revive an excluded entry. Reconsider only with new
facts resolving its rejection,
reconsider_excluded=true, an explanation and fresh operation references.
Data-only results must not claim a confirmed code cause. No private reasoning or raw business rows.
"""


def accept_entry(session: Any, entry: InvestigationEntry) -> InvestigationEntry:
    """复用阶段证据核验；只持久化摘要，不存 SQL 结果。历史入口不自动背书。"""
    checked = entry.model_copy(deep=True)
    checked.excluded_event_count = len(session.events) if checked.state == "excluded" else 0
    previous = session.investigation_entry
    exclusions: list[InvestigationEntry] = []
    for historical in [*session.entry_history, *([previous] if previous else [])]:
        if historical.state == "excluded":
            exclusions.append(historical)
        elif historical.state == "confirmed" and historical.reconsider_excluded:
            exclusions = [item for item in exclusions if not _same_target(item, historical)]
    root = Path(session.workspace).resolve()
    for value in checked.source_paths if checked.state != "excluded" else []:
        path = Path(value)
        windows = PureWindowsPath(value)
        if (
            path.is_absolute()
            or windows.drive
            or windows.root
            or ".." in windows.parts
            or not (root / path).resolve().is_relative_to(root)
            or not (root / path).is_file()
        ):
            checked.state = "candidate"
            checked.conflicts = [*checked.conflicts[:5], "源码路径不存在或不在当前工作区内。"]
            break
    assessment = WorkflowAssessment(
        phase=ProgressPhase.ANALYZING_REQUEST, reason=checked.reason, evidence=checked.evidence
    )
    proxy = SimpleNamespace(assessment=assessment, status=SimpleNamespace(value="needs_input"))
    checked.evidence = bind_assessment(session, proxy, mode="inspect").evidence
    # 改名不能绕过已排除的路径；恢复必须显式声明，并引用排除后取得的新证据。
    for excluded in exclusions if checked.state != "excluded" else []:
        if not _same_target(excluded, checked):
            continue
        fresh_event_ids = {event.id for event in session.events[excluded.excluded_event_count :]}
        new_evidence = any(
            item.verified and item.event_id in fresh_event_ids for item in checked.evidence
        )
        if not checked.reconsider_excluded or not new_evidence:
            checked.state = "candidate"
            checked.conflicts = [*checked.conflicts[:5], "该入口已被排除，需要新证据澄清排除原因。"]
            break
    if checked.state == "confirmed":
        refs = {
            item.reference.replace("\\", "/").casefold()
            for item in checked.evidence
            if item.kind == "code" and item.verified
        }
        has_identity = bool(checked.evidence) and all(item.verified for item in checked.evidence)
        if checked.kind == "data":
            has_identity = (
                has_identity
                and bool(checked.data_source and checked.data_object)
                and any(
                    item.kind == "query"
                    and item.verified
                    and any(
                        event.id == item.event_id
                        and any(
                            row.get("query_name", row.get("name")) == item.reference
                            and (
                                (row.get("source") or {}).get("datasource_id")
                                or row.get("datasource_id")
                            )
                            == checked.data_source
                            for row in event.data.get("queries", [])
                        )
                        for event in session.events
                    )
                    for item in checked.evidence
                )
            )
        else:
            has_identity = (
                has_identity
                and bool(checked.source_paths)
                and all(item.replace("\\", "/").casefold() in refs for item in checked.source_paths)
            )
        if checked.kind == "unknown" or checked.conflicts or not has_identity:
            checked.state = "candidate"
    if previous is not None and previous != checked:
        session.entry_history.append(previous.model_copy(deep=True))
    session.investigation_entry = checked
    return checked


def _same_target(left: InvestigationEntry, right: InvestigationEntry) -> bool:
    """排除记录与重新核验共用同一身份比较，别名不能绕过相同源码路径。"""
    left_paths = {value.replace("\\", "/").casefold() for value in left.source_paths}
    right_paths = {value.replace("\\", "/").casefold() for value in right.source_paths}
    return bool(
        left.kind == right.kind
        and (
            left.label.casefold() == right.label.casefold()
            or left_paths & right_paths
            or (
                left.data_object
                and left.data_object == right.data_object
                and left.data_source == right.data_source
            )
        )
    )


def entry_text(entry: InvestigationEntry | None) -> str:
    if entry is None:
        return ""
    kind = {
        "page": "页面",
        "api": "接口",
        "background": "后台任务",
        "data": "数据对象",
        "unknown": "未知入口",
    }[entry.kind]
    state = {"candidate": "待确认", "confirmed": "已确认", "excluded": "已排除"}[entry.state]
    return f"调查入口：{kind} · {entry.label} · {state}\n定位依据：{entry.reason}"
