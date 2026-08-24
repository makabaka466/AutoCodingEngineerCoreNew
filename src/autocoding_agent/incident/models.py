"""Stable contracts for page-aware, data-assisted incident investigation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints, model_validator

from autocoding_agent.core.models import AgentUsage, ChatMessage, utc_now
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
            if self.page is None:
                raise ValueError("page is required before querying incident data")
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
    page_hint: str | None = None
    database_reference: str | None = None
    source: str = "manual"
    external_reference: str | None = None
    runtime_session_id: str | None = None
    status: IncidentStatus | None = None
    last_decision: IncidentDecision | None = None
    last_usage: AgentUsage = Field(default_factory=AgentUsage)
    messages: list[ChatMessage] = Field(default_factory=list)
    query_observations: list[QueryObservation] = Field(default_factory=list)
    query_rounds: int = 0
    capability_document: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class IncidentOutcome(BaseModel):
    session_id: str
    workspace: str
    status: IncidentStatus
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
