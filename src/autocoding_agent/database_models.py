"""Database query contracts shared by development and incident workflows."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
QueryParameter = str | int | float | bool | None

# Shared conservative defaults for database evidence in both workflows.
DEFAULT_QUERY_MAX_ROWS = 100
DEFAULT_QUERY_TIMEOUT_SECONDS = 60


class DataQuery(BaseModel):
    """A minimal parameterized read-only query proposed by either workflow."""

    name: NonEmptyText
    purpose: NonEmptyText
    datasource_id: str | None = Field(
        default=None, description="Authorized project datasource ID. Required when ambiguous.",
    )
    query_kind: Literal["sql", "menu_catalog"] = Field(
        default="sql", description="sql 查询或配置好的完整菜单目录；目录无需模型编写 SQL。"
    )
    refresh_catalog: bool = Field(default=False, description="强制刷新菜单目录缓存。")
    sql: str = Field(
        default="",
        description=(
            "Parameterized read-only SQL. Use :name placeholders; the SQL Server host also "
            "accepts @name defensively and binds values without string interpolation."
        )
    )
    parameters: dict[str, QueryParameter] = Field(
        default_factory=dict,
        description="Values keyed by placeholder name, without ':' or '@'.",
    )
    max_rows: int = Field(default=DEFAULT_QUERY_MAX_ROWS, ge=1, le=DEFAULT_QUERY_MAX_ROWS)

    @model_validator(mode="after")
    def validate_query_kind(self):
        if self.query_kind == "sql" and not self.sql.strip():
            raise ValueError("sql 查询必须提供非空 SQL")
        if self.query_kind == "menu_catalog" and (self.sql.strip() or self.parameters):
            raise ValueError("菜单目录只使用已配置 SQL 和参数，请勿在请求中覆盖")
        if self.refresh_catalog and self.query_kind != "menu_catalog":
            raise ValueError("只有菜单目录支持刷新")
        return self


class QuerySource(BaseModel):
    """查询来源；环境是用户配置标签，不代表程序验证过部署环境。"""

    datasource_id: str = Field(description="稳定数据源 ID")
    name: str = Field(description="数据源展示名称")
    environment: str = Field(description="用户配置的环境标签")
    schema_name: str = Field(default="", description="预期 Schema/Owner，SQL 应显式限定")
    database_type: str = Field(default="sqlserver", description="数据库方言")


class QueryResult(BaseModel):
    """Bounded and sanitized data returned only to the active model turn."""

    query_name: str
    source: QuerySource | None = None
    columns: list[str]
    rows: list[dict[str, Any]]
    returned_rows: int
    truncated: bool = False
    redacted_columns: list[str] = Field(default_factory=list)
    catalog_info: dict[str, Any] | None = Field(
        default=None, description="目录来源、完整性及缓存摘要，不包含菜单行。"
    )


class QueryObservationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class QueryObservation(BaseModel):
    """Persisted audit metadata; raw database rows are intentionally excluded."""

    query_name: str
    source: QuerySource | None = None
    datasource_id: str | None = None
    purpose: str
    status: QueryObservationStatus = QueryObservationStatus.SUCCEEDED
    stage: str | None = None
    returned_rows: int = Field(default=0, ge=0)
    truncated: bool = False
    redacted_columns: list[str] = Field(default_factory=list)
    catalog_info: dict[str, Any] | None = Field(
        default=None, description="目录来源、完整性及缓存摘要，不包含菜单行。"
    )
    sql_fingerprint: str | None = None
    parameter_names: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def sql_fingerprint(sql: str) -> str:
    """Identify a query shape without storing SQL values or business result rows."""

    normalized = re.sub(r"\s+", " ", sql).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
