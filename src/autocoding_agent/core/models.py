"""Stable data contracts shared by the kernel and its adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, model_validator

from autocoding_agent.core.artifacts.models import ArtifactRecord
from autocoding_agent.core.audit.models import DecisionRecord, RiskLevel
from autocoding_agent.core.runtime.models import RuntimeRunRecord
from autocoding_agent.core.state_machine.models import CommandReceipt, TaskState
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


class AttachmentKind(StrEnum):
    IMAGE = "image"


class MessageAttachment(BaseModel):
    """A host-validated local file explicitly attached to one user message."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    kind: AttachmentKind = AttachmentKind.IMAGE
    path: NonEmptyText
    name: NonEmptyText
    media_type: NonEmptyText
    size_bytes: int = Field(ge=1, le=10 * 1024 * 1024)


class EventType(StrEnum):
    TASK_CREATED = "task_created"
    STATE_TRANSITIONED = "state_transitioned"
    TURN_STARTED = "turn_started"
    RUNTIME_FINISHED = "runtime_finished"
    RUNTIME_STARTED = "runtime_started"
    RUNTIME_ACTIVITY = "runtime_activity"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    CODE_MODIFIED = "code_modified"
    TEST_EXECUTED = "test_executed"
    VERIFICATION_FAILED = "verification_failed"
    RUNTIME_COMPLETED = "runtime_completed"
    RUNTIME_FAILED = "runtime_failed"
    RUNTIME_INTERRUPTED = "runtime_interrupted"
    RECOVERY_REQUIRED = "recovery_required"
    DECISION_RECORDED = "decision_recorded"
    ARTIFACT_RECORDED = "artifact_recorded"
    ARTIFACT_FAILED = "artifact_failed"
    INPUT_REQUIRED = "input_required"
    APPROVAL_REQUIRED = "approval_required"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    CAPABILITY_SAVED = "capability_saved"
    CAPABILITY_FAILED = "capability_failed"
    DATABASE_QUERIES_EXECUTED = "database_queries_executed"
    DATABASE_QUERY_FAILED = "database_query_failed"
    KNOWLEDGE_RETRIEVED = "knowledge_retrieved"
    KNOWLEDGE_RETRIEVAL_FAILED = "knowledge_retrieval_failed"
    TASK_REOPENED = "task_reopened"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    attachments: list[MessageAttachment] = Field(default_factory=list, max_length=5)
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
    reason: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_level: RiskLevel | None = None
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
    sequence: int | None = Field(default=None, ge=1)
    schema_version: int = Field(default=1, ge=1)
    type: EventType
    message: str
    reason: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_level: RiskLevel | None = None
    actor: str = "host"
    correlation_id: str | None = None
    causation_id: str | None = None
    command_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class AgentSession(BaseModel):
    """Persistent state for one user task and its clarification/approval turns."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    workspace: str
    goal: str
    project: str | None = None
    task_state: TaskState = TaskState.CREATED
    version: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)
    runtime_session_id: str | None = None
    status: AgentStatus | None = None
    pending_approval: ApprovalRequest | None = None
    last_decision: AgentDecision | None = None
    last_usage: AgentUsage = Field(default_factory=AgentUsage)
    capability_document: str | None = None
    database_reference: str | None = None
    query_observations: list[QueryObservation] = Field(default_factory=list)
    query_rounds: int = 0
    replan_rounds: int = 0
    cycle_number: int = Field(default=1, ge=1)
    cycle_objective: str | None = None
    cycle_query_observation_start: int = Field(default=0, ge=0)
    messages: list[ChatMessage] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    decision_records: list[DecisionRecord] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    runs: list[RuntimeRunRecord] = Field(default_factory=list)
    command_receipts: list[CommandReceipt] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def infer_task_state_for_legacy_sessions(cls, data: Any) -> Any:
        """Load pre-state-machine JSON without rewriting the original session file."""

        if not isinstance(data, dict) or "task_state" in data:
            return data
        restored = {**data}
        status = str(restored.get("status") or "")
        approval = restored.get("pending_approval")
        if status == AgentStatus.NEEDS_INPUT.value:
            task_state = TaskState.WAITING_INPUT
        elif status == AgentStatus.QUERY_REQUIRED.value:
            task_state = TaskState.QUERYING_DATA
        elif status == AgentStatus.APPROVAL_REQUIRED.value:
            scope = (
                approval.scope.value
                if isinstance(approval, ApprovalRequest)
                else str((approval or {}).get("scope") or "")
            )
            task_state = (
                TaskState.WAITING_VERIFY_APPROVAL
                if scope == ApprovalScope.VERIFY.value
                else TaskState.WAITING_MODIFY_APPROVAL
            )
        elif status == AgentStatus.COMPLETED.value:
            task_state = TaskState.COMPLETED
        elif status == AgentStatus.FAILED.value:
            task_state = TaskState.FAILED
        else:
            task_state = TaskState.CREATED
        restored["task_state"] = task_state
        restored.setdefault("version", 0)
        restored.setdefault("revision", 0)
        return restored

    @model_validator(mode="after")
    def validate_cycle_observation_offset(self) -> AgentSession:
        if self.cycle_query_observation_start > len(self.query_observations):
            raise ValueError("cycle query observation offset exceeds stored observations")
        return self


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
    additional_dirs: list[str] = Field(default_factory=list, max_length=5)


class RuntimeResult(BaseModel):
    decision: AgentDecision
    runtime_session_id: str
    usage: AgentUsage = Field(default_factory=AgentUsage)


class AgentOutcome(BaseModel):
    session_id: str
    workspace: str
    status: AgentStatus
    task_state: TaskState = TaskState.CREATED
    cycle_number: int = Field(default=1, ge=1)
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
