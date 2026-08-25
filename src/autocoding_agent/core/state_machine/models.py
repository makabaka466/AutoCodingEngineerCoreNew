"""Lifecycle contracts kept separate from model decisions and runtime permissions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskState(StrEnum):
    """Durable lifecycle state for one software-development task."""

    CREATED = "created"
    INSPECTING = "inspecting"
    WAITING_INPUT = "waiting_input"
    QUERYING_DATA = "querying_data"
    WAITING_MODIFY_APPROVAL = "waiting_modify_approval"
    IMPLEMENTING = "implementing"
    WAITING_VERIFY_APPROVAL = "waiting_verify_approval"
    VERIFYING = "verifying"
    REPLANNING = "replanning"
    PAUSED = "paused"
    RECOVERY_REQUIRED = "recovery_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentCommandType(StrEnum):
    """External intents that may drive a task transition."""

    CREATE_TASK = "create_task"
    SUBMIT_USER_INPUT = "submit_user_input"
    GRANT_APPROVAL = "grant_approval"
    REJECT_APPROVAL = "reject_approval"
    RESUME_TASK = "resume_task"
    PAUSE_TASK = "pause_task"
    CANCEL_TASK = "cancel_task"


class FailureClass(StrEnum):
    """Failure categories used by later recovery policy phases."""

    RUNTIME_TRANSIENT = "runtime_transient"
    PROVIDER = "provider"
    POLICY = "policy"
    VALIDATION = "validation"
    SIDE_EFFECT_UNCERTAIN = "side_effect_uncertain"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class TransitionRule:
    """One source state and every lifecycle state it may enter."""

    source: TaskState
    targets: frozenset[TaskState]


class AgentCommand(BaseModel):
    """Idempotent command envelope for future external and recovery entry points."""

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=100)
    task_id: str
    type: AgentCommandType
    expected_version: int = Field(ge=0)
    actor: str = "user"
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CommandReceipt(BaseModel):
    """Terminal acknowledgement used to make external command retries idempotent."""

    command_id: str
    task_id: str
    command_type: AgentCommandType
    outcome_status: str
    outcome_message: str
    task_state: TaskState
    completed_version: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
