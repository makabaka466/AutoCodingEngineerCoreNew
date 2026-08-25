"""Stable contracts for page-aware, data-assisted incident investigation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, model_validator

from autocoding_agent.core.models import AgentEvent, AgentUsage, ChatMessage, utc_now
from autocoding_agent.core.runtime.models import RuntimeRunRecord
from autocoding_agent.core.state_machine.models import CommandReceipt, TaskState
from autocoding_agent.database_models import DataQuery, QueryObservation, QueryResult

__all__ = [
    "DataQuery",
    "IncidentDecision",
    "IncidentFinding",
    "IncidentOutcome",
    "IncidentSession",
    "IncidentStatus",
    "LocatedPage",
    "QueryObservation",
    "QueryResult",
]

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class IncidentStatus(StrEnum):
    NEEDS_INPUT = "needs_input"
    QUERY_REQUIRED = "query_required"
    COMPLETED = "completed"
    FAILED = "failed"


class LocatedPage(BaseModel):
    """The page and related code located by the model."""

    name: NonEmptyText
    route: NonEmptyText | None = None
    source_paths: list[NonEmptyText] = Field(default_factory=list)
    related_paths: list[NonEmptyText] = Field(default_factory=list)
    explanation: NonEmptyText


class IncidentFinding(BaseModel):
    summary: NonEmptyText
    evidence: list[NonEmptyText] = Field(default_factory=list)


class IncidentDecision(BaseModel):
    """One model decision in the incident investigation state machine."""

    status: IncidentStatus
    message: NonEmptyText
    question: NonEmptyText | None = None
    page: LocatedPage | None = None
    queries: list[DataQuery] = Field(default_factory=list, max_length=5)
    diagnosis: NonEmptyText | None = None
    findings: list[IncidentFinding] = Field(default_factory=list)
    recommended_actions: list[NonEmptyText] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    automation_candidate: bool = False

    @model_validator(mode="after")
    def validate_status_payload(self) -> IncidentDecision:
        if self.status == IncidentStatus.NEEDS_INPUT and self.question is None:
            raise ValueError("question is required when status is needs_input")
        if self.status == IncidentStatus.QUERY_REQUIRED:
            if not self.queries:
                raise ValueError("queries are required when status is query_required")
        elif self.queries:
            raise ValueError("queries are only valid when status is query_required")
        if self.status == IncidentStatus.COMPLETED:
            if self.page is None:
                raise ValueError("page is required when incident investigation is completed")
            if self.diagnosis is None:
                raise ValueError("diagnosis is required when incident investigation is completed")
        return self


class IncidentSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    workspace: str
    problem: str
    project: str | None = None
    page_hint: str | None = None
    database_reference: str | None = None
    source: str = "manual"
    external_reference: str | None = None
    task_state: TaskState = TaskState.CREATED
    version: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)
    runtime_session_id: str | None = None
    status: IncidentStatus | None = None
    last_decision: IncidentDecision | None = None
    last_usage: AgentUsage = Field(default_factory=AgentUsage)
    messages: list[ChatMessage] = Field(default_factory=list)
    query_observations: list[QueryObservation] = Field(default_factory=list)
    query_rounds: int = 0
    cycle_number: int = Field(default=1, ge=1)
    cycle_objective: str | None = None
    cycle_query_observation_start: int = Field(default=0, ge=0)
    capability_document: str | None = None
    events: list[AgentEvent] = Field(default_factory=list)
    runs: list[RuntimeRunRecord] = Field(default_factory=list)
    command_receipts: list[CommandReceipt] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def infer_lifecycle_for_legacy_sessions(cls, data: Any) -> Any:
        """Load pre-runtime incident JSON without mutating the source file."""

        if not isinstance(data, dict) or "task_state" in data:
            return data
        restored = {**data}
        status = str(restored.get("status") or "")
        restored["task_state"] = {
            IncidentStatus.NEEDS_INPUT.value: TaskState.WAITING_INPUT,
            IncidentStatus.QUERY_REQUIRED.value: TaskState.QUERYING_DATA,
            IncidentStatus.COMPLETED.value: TaskState.COMPLETED,
            IncidentStatus.FAILED.value: TaskState.FAILED,
        }.get(status, TaskState.CREATED)
        restored.setdefault("version", 0)
        restored.setdefault("revision", 0)
        restored.setdefault("events", [])
        restored.setdefault("runs", [])
        restored.setdefault("command_receipts", [])
        return restored

    @model_validator(mode="after")
    def validate_cycle_observation_offset(self) -> IncidentSession:
        if self.cycle_query_observation_start > len(self.query_observations):
            raise ValueError("cycle query observation offset exceeds stored observations")
        return self


class IncidentOutcome(BaseModel):
    session_id: str
    workspace: str
    status: IncidentStatus
    task_state: TaskState = TaskState.CREATED
    cycle_number: int = Field(default=1, ge=1)
    message: str
    question: str | None = None
    page: LocatedPage | None = None
    diagnosis: str | None = None
    findings: list[IncidentFinding] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    confidence: float | None = None
    automation_candidate: bool = False
    query_observations: list[QueryObservation] = Field(default_factory=list)
    capability_document: str | None = None
    usage: AgentUsage = Field(default_factory=AgentUsage)
    events: list[AgentEvent] = Field(default_factory=list)
