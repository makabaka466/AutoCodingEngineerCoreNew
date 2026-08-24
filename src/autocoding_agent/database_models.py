"""Database query contracts shared by development and incident workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
QueryParameter = str | int | float | bool | None


class DataQuery(BaseModel):
    """A minimal parameterized read-only query proposed by either workflow."""

    name: NonEmptyText
    purpose: NonEmptyText
    sql: NonEmptyText
    parameters: dict[str, QueryParameter] = Field(default_factory=dict)
    max_rows: int = Field(default=20, ge=1, le=100)


class QueryResult(BaseModel):
    """Bounded and sanitized data returned only to the active model turn."""

    query_name: str
    columns: list[str]
    rows: list[dict[str, Any]]
    returned_rows: int
    truncated: bool = False
    redacted_columns: list[str] = Field(default_factory=list)


class QueryObservation(BaseModel):
    """Persisted audit metadata; raw database rows are intentionally excluded."""

    query_name: str
    purpose: str
    returned_rows: int
    truncated: bool
    redacted_columns: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
