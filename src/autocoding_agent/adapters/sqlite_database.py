"""Read-only SQLite adapter for the incident investigation workflow."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from autocoding_agent.adapters.database_safety import (
    ReadOnlyQueryError,
    safe_value,
    sensitive_columns,
    validate_read_only_sql,
)
from autocoding_agent.database_models import DataQuery, QueryResult


class SQLiteDatabaseReader:
    """Expose schema metadata and bounded queries through a read-only connection."""

    def __init__(
        self,
        path: str | Path,
        max_rows: int = 50,
        query_timeout_seconds: int = 5,
    ) -> None:
        resolved = Path(path).expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"SQLite database is not a file: {resolved}")
        if max_rows < 1 or max_rows > 1000:
            raise ValueError("max_rows must be between 1 and 1000")
        if query_timeout_seconds < 1 or query_timeout_seconds > 60:
            raise ValueError("query_timeout_seconds must be between 1 and 60")
        self.path = resolved
        self.max_rows = max_rows
        self.query_timeout_seconds = query_timeout_seconds

    def describe_schema(self) -> str:
        """Describe user tables and columns without including business data."""

        with closing(self._connect()) as connection:
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 100"
            ).fetchall()
            descriptions: list[str] = []
            for (table_name,) in table_rows:
                escaped = str(table_name).replace('"', '""')
                columns = connection.execute(
                    f'PRAGMA table_info("{escaped}")'  # noqa: S608 - quoted schema identifier
                ).fetchall()
                rendered = ", ".join(f"{item[1]} {item[2] or 'ANY'}" for item in columns[:100])
                descriptions.append(f"{table_name}({rendered})")
        rendered = "\n".join(descriptions) or "No user tables were found."
        return f"Database dialect: SQLite.\n{rendered}"

    def execute(self, query: DataQuery) -> QueryResult:
        validate_read_only_sql(query.sql, allow_explain=True)

        row_limit = min(query.max_rows, self.max_rows)
        with closing(self._connect()) as connection:
            connection.set_authorizer(_read_only_authorizer)
            deadline = time.monotonic() + self.query_timeout_seconds
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline),
                1000,
            )
            try:
                cursor = connection.execute(query.sql, query.parameters)
                raw_rows = cursor.fetchmany(row_limit + 1)
            except sqlite3.DatabaseError as exc:
                raise ReadOnlyQueryError(f"Read-only query failed: {exc}") from exc

        columns = [item[0] for item in (cursor.description or [])]
        sensitive = sensitive_columns(columns)
        visible_rows = raw_rows[:row_limit]
        rows = [
            {
                column: "[REDACTED]" if column in sensitive else safe_value(value)
                for column, value in zip(columns, row, strict=True)
            }
            for row in visible_rows
        ]
        return QueryResult(
            query_name=query.name,
            columns=columns,
            rows=rows,
            returned_rows=len(rows),
            truncated=len(raw_rows) > row_limit,
            redacted_columns=sensitive,
        )

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.execute("PRAGMA query_only = ON")
        return connection


def _read_only_authorizer(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _database: str | None,
    _trigger: str | None,
) -> int:
    blocked_names = (
        "SQLITE_INSERT",
        "SQLITE_UPDATE",
        "SQLITE_DELETE",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_DETACH",
        "SQLITE_TRANSACTION",
        "SQLITE_SAVEPOINT",
        "SQLITE_PRAGMA",
    )
    blocked = {getattr(sqlite3, name) for name in blocked_names if hasattr(sqlite3, name)}
    return sqlite3.SQLITE_DENY if action in blocked else sqlite3.SQLITE_OK
