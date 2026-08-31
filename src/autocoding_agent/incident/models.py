"""Stable contracts for page-aware, data-assisted incident investigation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, model_validator

from autocoding_agent.core.artifacts.models import ArtifactRecord
from autocoding_agent.core.hermes import HermesSkillObservation, HermesSkillRequest
from autocoding_agent.core.models import AgentEvent, AgentUsage, ChatMessage, utc_now
from autocoding_agent.core.runtime.models import RuntimeRunRecord
from autocoding_agent.core.state_machine.models import CommandReceipt, TaskState
from autocoding_agent.database_models import (
    DataQuery,
    QueryObservation,
    QueryObservationStatus,
    QueryResult,
)

__all__ = [
    "DataQuery",
    "IncidentDecision",
    "IncidentFinding",
    "IncidentOutcome",
    "IncidentQueryStage",
    "IncidentSession",
    "IncidentStatus",
    "LocatedPage",
    "QueryObservation",
    "QueryObservationStatus",
    "QueryResult",
]

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class IncidentStatus(StrEnum):
    NEEDS_INPUT = "needs_input"
    QUERY_REQUIRED = "query_required"
    HERMES_SKILL_REQUIRED = "hermes_skill_required"
    COMPLETED = "completed"
    FAILED = "failed"


class IncidentQueryStage(StrEnum):
    """Model-selected database purpose used only for deterministic host budgets."""

    PAGE_LOOKUP = "page_lookup"
    BUSINESS_DATA = "business_data"


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
    message: NonEmptyText = Field(
        description=(
            "Concise user-facing summary. For completed decisions, state the final conclusion "
            "in one sentence; put the causal explanation in diagnosis."
        )
    )
    question: NonEmptyText | None = None
    page: LocatedPage | None = Field(
        default=None,
        description=(
            "Required for business_data and completed decisions. Repeat the verified page "
            "identity and workspace-relative source paths on every such decision."
        ),
    )
    query_stage: IncidentQueryStage | None = Field(
        default=None,
        description=(
            "Required when status is query_required. Use page_lookup while resolving the "
            "page/menu mapping; use business_data only after page source code is verified."
        ),
    )
    queries: list[DataQuery] = Field(default_factory=list, max_length=5)
    diagnosis: NonEmptyText | None = Field(
        default=None,
        description=(
            "Why the incident happened, including the evidence-backed causal chain and an "
            "explicit certainty level when the root cause is not fully proven."
        ),
    )
    findings: list[IncidentFinding] = Field(default_factory=list)
    recommended_actions: list[NonEmptyText] = Field(
        default_factory=list,
        description=(
            "Concrete solutions or safe verification steps. At least one is required when the "
            "incident is completed."
        ),
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    automation_candidate: bool = False
    hermes_skill: HermesSkillRequest | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> IncidentDecision:
        if self.status == IncidentStatus.NEEDS_INPUT and self.question is None:
            raise ValueError("question is required when status is needs_input")
        if self.status == IncidentStatus.QUERY_REQUIRED:
            if not self.queries:
                raise ValueError("queries are required when status is query_required")
            if self.query_stage is None:
                raise ValueError("query_stage is required when status is query_required")
        elif self.queries:
            raise ValueError("queries are only valid when status is query_required")
        elif self.query_stage is not None:
            raise ValueError("query_stage is only valid when status is query_required")
        if self.status == IncidentStatus.HERMES_SKILL_REQUIRED and self.hermes_skill is None:
            raise ValueError(
                "hermes_skill is required when status is hermes_skill_required"
            )
        if self.status != IncidentStatus.HERMES_SKILL_REQUIRED and self.hermes_skill is not None:
            raise ValueError(
                "hermes_skill is only valid when status is hermes_skill_required"
            )
        if self.status == IncidentStatus.COMPLETED:
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
    located_page: LocatedPage | None = None
    last_usage: AgentUsage = Field(default_factory=AgentUsage)
    messages: list[ChatMessage] = Field(default_factory=list)
    query_observations: list[QueryObservation] = Field(default_factory=list)
    hermes_skill_observations: list[HermesSkillObservation] = Field(default_factory=list)
    query_rounds: int = 0
    page_query_rounds: int = Field(default=0, ge=0)
    business_query_rounds: int = Field(default=0, ge=0)
    query_repair_rounds: int = Field(default=0, ge=0)
    cycle_number: int = Field(default=1, ge=1)
    cycle_objective: str | None = None
    cycle_query_observation_start: int = Field(default=0, ge=0)
    cycle_hermes_observation_start: int = Field(default=0, ge=0)
    capability_document: str | None = None
    events: list[AgentEvent] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    runs: list[RuntimeRunRecord] = Field(default_factory=list)
    command_receipts: list[CommandReceipt] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def infer_lifecycle_for_legacy_sessions(cls, data: Any) -> Any:
        """Load pre-runtime incident JSON without mutating the source file."""

        if not isinstance(data, dict):
            return data
        restored = {**data}
        if "task_state" not in restored:
            status = str(restored.get("status") or "")
            restored["task_state"] = {
                IncidentStatus.NEEDS_INPUT.value: TaskState.WAITING_INPUT,
                IncidentStatus.QUERY_REQUIRED.value: TaskState.QUERYING_DATA,
                IncidentStatus.COMPLETED.value: TaskState.COMPLETED,
                IncidentStatus.FAILED.value: TaskState.FAILED,
            }.get(status, TaskState.CREATED)
        last_decision = restored.get("last_decision")
        if (
            isinstance(last_decision, dict)
            and str(last_decision.get("status") or "") == IncidentStatus.QUERY_REQUIRED.value
            and "query_stage" not in last_decision
        ):
            restored["last_decision"] = {
                **last_decision,
                "query_stage": (
                    IncidentQueryStage.BUSINESS_DATA.value
                    if last_decision.get("page")
                    else IncidentQueryStage.PAGE_LOOKUP.value
                ),
            }
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
        if self.cycle_hermes_observation_start > len(self.hermes_skill_observations):
            raise ValueError("cycle Hermes observation offset exceeds stored observations")
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
    hermes_skill_observations: list[HermesSkillObservation] = Field(default_factory=list)
    capability_document: str | None = None
    usage: AgentUsage = Field(default_factory=AgentUsage)
    events: list[AgentEvent] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
