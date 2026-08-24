"""Stable data contracts shared by the kernel and its adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, model_validator

from autocoding_agent.database_models import DataQuery, QueryObservation

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentStatus(StrEnum):
    NEEDS_INPUT = "needs_input"
    QUERY_REQUIRED = "query_required"
    APPROVAL_REQUIRED = "approval_required"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentMode(StrEnum):
    INSPECT = "inspect"
    IMPLEMENT = "implement"
    VERIFY = "verify"


class ApprovalScope(StrEnum):
    MODIFY = "modify"
    VERIFY = "verify"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class EventType(StrEnum):
    TURN_STARTED = "turn_started"
    RUNTIME_FINISHED = "runtime_finished"
    INPUT_REQUIRED = "input_required"
    APPROVAL_REQUIRED = "approval_required"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    CAPABILITY_SAVED = "capability_saved"
    CAPABILITY_FAILED = "capability_failed"
    DATABASE_QUERIES_EXECUTED = "database_queries_executed"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class Evidence(BaseModel):
    """A concise, traceable fact supporting the model's conclusion."""

    path: str | None = None
    summary: str


class ProposedChange(BaseModel):
    """One concrete before/after item in a model-authored change proposal."""

    path: NonEmptyText | None = None
    area: NonEmptyText
    current: NonEmptyText
    proposed: NonEmptyText


class ChangeProposal(BaseModel):
    """The plan a user reviews before granting repository edit permission."""

    summary: NonEmptyText
    changes: list[ProposedChange] = Field(min_length=1)
    expected_result: NonEmptyText
    impact: list[NonEmptyText] = Field(default_factory=list)
    validation: list[NonEmptyText] = Field(default_factory=list)
    preview_markdown: NonEmptyText | None = None


class ApprovalRequest(BaseModel):
    scope: ApprovalScope
    reason: str
    proposed_actions: list[str] = Field(default_factory=list)
    proposal: ChangeProposal | None = Field(
        description=(
            "Required for modify scope: the complete plan shown before repository edits. "
            "Use null for verify scope."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_missing_proposal(cls, data: Any) -> Any:
        """Old saved approvals predate the required proposal key; load them as null."""

        if isinstance(data, dict) and "proposal" not in data:
            data = {**data, "proposal": None}
        return data


class CapabilityDraft(BaseModel):
    """Reusable knowledge distilled by the model in the task's final turn."""

    title: str
    summary: str
    triggers: list[str] = Field(default_factory=list)
    method: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class AgentDecision(BaseModel):
    """The only machine-readable decision accepted from the model runtime."""

    status: AgentStatus
    message: str
    evidence: list[Evidence] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    approval: ApprovalRequest | None = None
    changed_files: list[str] = Field(default_factory=list)
    test_summary: str | None = None
    capability: CapabilityDraft | None = None
    queries: list[DataQuery] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_status_payload(self) -> AgentDecision:
        if self.status == AgentStatus.APPROVAL_REQUIRED and self.approval is None:
            raise ValueError("approval is required when status is approval_required")
        if self.status != AgentStatus.APPROVAL_REQUIRED and self.approval is not None:
            raise ValueError("approval is only valid when status is approval_required")
        if self.status == AgentStatus.QUERY_REQUIRED and not self.queries:
            raise ValueError("queries are required when status is query_required")
        if self.status != AgentStatus.QUERY_REQUIRED and self.queries:
            raise ValueError("queries are only valid when status is query_required")
        return self


class AgentUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    turns: int | None = None


class AgentEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: EventType
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class AgentSession(BaseModel):
    """Persistent state for one user task and its clarification/approval turns."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    workspace: str
    goal: str
    project: str | None = None
    runtime_session_id: str | None = None
    status: AgentStatus | None = None
    pending_approval: ApprovalRequest | None = None
    last_decision: AgentDecision | None = None
    last_usage: AgentUsage = Field(default_factory=AgentUsage)
    capability_document: str | None = None
    database_reference: str | None = None
    query_observations: list[QueryObservation] = Field(default_factory=list)
    query_rounds: int = 0
    messages: list[ChatMessage] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RuntimeTurn(BaseModel):
    session_id: str
    runtime_session_id: str | None = None
    workspace: str
    user_message: str
    history: list[ChatMessage] = Field(default_factory=list)
    mode: AgentMode
    system_prompt: str
    tools: list[str]
    allowed_tools: list[str]
    permission_mode: str = "dontAsk"
    capability_dir: str | None = None


class RuntimeResult(BaseModel):
    decision: AgentDecision
    runtime_session_id: str
    usage: AgentUsage = Field(default_factory=AgentUsage)


class AgentOutcome(BaseModel):
    session_id: str
    workspace: str
    status: AgentStatus
    message: str
    evidence: list[Evidence] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    approval: ApprovalRequest | None = None
    changed_files: list[str] = Field(default_factory=list)
    test_summary: str | None = None
    capability_document: str | None = None
    query_observations: list[QueryObservation] = Field(default_factory=list)
    usage: AgentUsage = Field(default_factory=AgentUsage)
    events: list[AgentEvent] = Field(default_factory=list)
