"""Observable Runtime lifecycle contracts used for audit and recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from autocoding_agent.core.state_machine.models import TaskState


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeEventKind(StrEnum):
    SYSTEM_INIT = "system_init"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    HEARTBEAT = "heartbeat"
    PROTOCOL_WARNING = "protocol_warning"


class RunStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RuntimeActivity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    kind: RuntimeEventKind
    summary: str
    tool_name: str | None = None
    tool_use_id: str | None = None
    parent_tool_use_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)


class RuntimeRunRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    state: TaskState
    mode: str
    owner_id: str | None = None
    owner_pid: int | None = Field(default=None, ge=1)
    status: RunStatus = RunStatus.STARTED
    started_at: datetime = Field(default_factory=_utc_now)
    heartbeat_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    terminal_reason: str | None = None
    runtime_session_id: str | None = None
    activity_ids: list[str] = Field(default_factory=list)
