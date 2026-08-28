"""Safe, workflow-neutral progress events for interactive delivery surfaces."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePath

from pydantic import BaseModel, Field

from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind

logger = logging.getLogger("autocoding_agent.progress")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProgressWorkflow(StrEnum):
    DEVELOPMENT = "development"
    INCIDENT = "incident"


class ProgressPhase(StrEnum):
    PREPARING_CONTEXT = "preparing_context"
    RETRIEVING_KNOWLEDGE = "retrieving_knowledge"
    ANALYZING_REQUEST = "analyzing_request"
    ANALYZING_IMAGE = "analyzing_image"
    LOCATING_PAGE = "locating_page"
    VERIFYING_PAGE = "verifying_page"
    INSPECTING_CODE = "inspecting_code"
    QUERYING_DATABASE = "querying_database"
    DIAGNOSING_CAUSE = "diagnosing_cause"
    PLANNING_CHANGE = "planning_change"
    MODIFYING_CODE = "modifying_code"
    VERIFYING_CHANGE = "verifying_change"
    SAVING_CAPABILITY = "saving_capability"
    RECOVERING = "recovering"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


PROGRESS_LABELS: dict[ProgressPhase, str] = {
    ProgressPhase.PREPARING_CONTEXT: "正在准备任务上下文",
    ProgressPhase.RETRIEVING_KNOWLEDGE: "正在检索项目知识",
    ProgressPhase.ANALYZING_REQUEST: "正在分析任务需求",
    ProgressPhase.ANALYZING_IMAGE: "正在分析异常截图",
    ProgressPhase.LOCATING_PAGE: "正在定位异常页面",
    ProgressPhase.VERIFYING_PAGE: "正在核对页面线索",
    ProgressPhase.INSPECTING_CODE: "正在阅读相关代码",
    ProgressPhase.QUERYING_DATABASE: "正在查询业务数据",
    ProgressPhase.DIAGNOSING_CAUSE: "正在分析异常原因",
    ProgressPhase.PLANNING_CHANGE: "正在制定修改方案",
    ProgressPhase.MODIFYING_CODE: "正在修改代码",
    ProgressPhase.VERIFYING_CHANGE: "正在验证修改结果",
    ProgressPhase.SAVING_CAPABILITY: "正在沉淀本轮能力",
    ProgressPhase.RECOVERING: "正在恢复任务上下文",
    ProgressPhase.WAITING_INPUT: "等待补充关键信息",
    ProgressPhase.WAITING_APPROVAL: "等待确认修改方案",
    ProgressPhase.COMPLETED: "本轮任务已完成",
    ProgressPhase.FAILED: "本轮处理未完成",
}


class ProgressEvent(BaseModel):
    """A curated status update; never a model chain-of-thought transcript."""

    task_id: str | None = None
    workflow: ProgressWorkflow
    phase: ProgressPhase
    label: str
    detail: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=_utc_now)

    @classmethod
    def for_phase(
        cls,
        workflow: ProgressWorkflow,
        phase: ProgressPhase,
        *,
        task_id: str | None = None,
        detail: str | None = None,
        active: bool = True,
    ) -> ProgressEvent:
        return cls(
            task_id=task_id,
            workflow=workflow,
            phase=phase,
            label=PROGRESS_LABELS[phase],
            detail=_safe_detail(detail),
            active=active,
        )


ProgressSink = Callable[[ProgressEvent], None]


def emit_progress(sink: ProgressSink | None, event: ProgressEvent) -> None:
    """Notify a delivery adapter without allowing UI failures to stop the task."""

    if sink is None:
        return
    try:
        sink(event)
    except Exception:
        logger.warning("progress_sink_failed phase=%s", event.phase.value, exc_info=True)


class ProgressProjector:
    """Project sanitized Runtime evidence into stable, user-facing phases."""

    _READ_TOOLS = {"read"}
    _SEARCH_TOOLS = {"glob", "grep"}
    _WRITE_TOOLS = {"edit", "write", "notebookedit"}
    _IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    @classmethod
    def from_runtime(
        cls,
        activity: RuntimeActivity,
        *,
        workflow: ProgressWorkflow,
        task_id: str,
        mode: str,
        attachment_paths: Collection[str] = (),
    ) -> ProgressEvent | None:
        if activity.kind == RuntimeEventKind.SYSTEM_INIT:
            return ProgressEvent.for_phase(
                workflow,
                ProgressPhase.ANALYZING_REQUEST,
                task_id=task_id,
            )
        if activity.kind == RuntimeEventKind.HEARTBEAT:
            return ProgressEvent.for_phase(
                workflow,
                cls._phase_for_mode(mode, workflow),
                task_id=task_id,
                detail="模型仍在处理",
            )
        if activity.kind != RuntimeEventKind.TOOL_STARTED:
            return None

        tool = (activity.tool_name or "").casefold()
        safe_path = str(activity.data.get("path") or "")
        if tool in cls._READ_TOOLS:
            phase = (
                ProgressPhase.ANALYZING_IMAGE
                if cls._is_attachment_image(safe_path, attachment_paths)
                else ProgressPhase.INSPECTING_CODE
            )
        elif tool in cls._SEARCH_TOOLS:
            phase = (
                ProgressPhase.LOCATING_PAGE
                if workflow == ProgressWorkflow.INCIDENT
                else ProgressPhase.INSPECTING_CODE
            )
        elif tool in cls._WRITE_TOOLS:
            phase = ProgressPhase.MODIFYING_CODE
        elif tool == "bash":
            phase = (
                ProgressPhase.VERIFYING_CHANGE
                if mode == "verify"
                else cls._phase_for_mode(mode, workflow)
            )
        else:
            return None
        return ProgressEvent.for_phase(
            workflow,
            phase,
            task_id=task_id,
            detail=_path_detail(safe_path),
        )

    @staticmethod
    def _phase_for_mode(mode: str, workflow: ProgressWorkflow) -> ProgressPhase:
        if mode == "implement":
            return ProgressPhase.MODIFYING_CODE
        if mode == "verify":
            return ProgressPhase.VERIFYING_CHANGE
        if workflow == ProgressWorkflow.INCIDENT:
            return ProgressPhase.DIAGNOSING_CAUSE
        return ProgressPhase.ANALYZING_REQUEST

    @classmethod
    def _is_attachment_image(
        cls,
        path: str,
        attachment_paths: Collection[str],
    ) -> bool:
        if not path or PurePath(path).suffix.casefold() not in cls._IMAGE_SUFFIXES:
            return False
        normalized = path.replace("\\", "/").casefold()
        return any(
            normalized == item.replace("\\", "/").casefold()
            or normalized.endswith("/" + PurePath(item).name.casefold())
            for item in attachment_paths
        )


def _path_detail(value: str) -> str | None:
    if not value or value == "<OUTSIDE_WORKSPACE>":
        return None
    return _safe_detail(PurePath(value).name)


def _safe_detail(value: str | None) -> str | None:
    compact = " ".join((value or "").split())[:120]
    return compact or None
