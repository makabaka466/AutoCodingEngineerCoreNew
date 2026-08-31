"""Bounded, read-only SQL Server adapter for incident investigation."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from contextlib import closing
from typing import Any

import pyodbc

from autocoding_agent.adapters.database_safety import (
    ReadOnlyQueryError,
    safe_value,
    sensitive_columns,
    validate_read_only_sql,
)
from autocoding_agent.database_models import (
    DEFAULT_QUERY_MAX_ROWS,
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    DataQuery,
    QueryResult,
)
from autocoding_agent.sqlserver_config import (
    SQLServerAuthentication,
    SQLServerConnectionConfig,
)

Connector = Callable[..., Any]


def available_sqlserver_drivers(
    supplier: Callable[[], Sequence[str]] = pyodbc.drivers,
) -> list[str]:
    """Return installed SQL Server ODBC drivers, newest names first."""

    drivers = [name for name in supplier() if "sql server" in name.casefold()]

    def priority(name: str) -> tuple[int, int, str]:
        versions = re.findall(r"\d+", name)
        version = int(versions[-1]) if versions else 0
        return (int("odbc driver" in name.casefold()), version, name)

    return sorted(drivers, key=priority, reverse=True)


class SQLServerDatabaseReader:
    """Read SQL Server through ODBC without exposing credentials to the model."""

    def __init__(
        self,
        config: SQLServerConnectionConfig,
        password: str | None = None,
        *,
        max_rows: int = DEFAULT_QUERY_MAX_ROWS,
        query_timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS,
        connector: Connector = pyodbc.connect,
    ) -> None:
        if max_rows < 1 or max_rows > 1000:
            raise ValueError("max_rows must be between 1 and 1000")
        if query_timeout_seconds < 1 or query_timeout_seconds > 60:
            raise ValueError("query_timeout_seconds must be between 1 and 60")
        if config.authentication == SQLServerAuthentication.SQL_PASSWORD:
            if not config.username or not password:
                raise ValueError("SQL Server username and password are required.")
        self.config = config
        self._password = password
        self.max_rows = max_rows
        self.query_timeout_seconds = query_timeout_seconds
        self._connector = connector

    @property
    def reference(self) -> str:
        return self.config.reference

    def test_connection(self) -> str:
        """Open the configured database and perform a harmless metadata query."""

        with closing(self._connect()) as connection:
            connection.timeout = self.query_timeout_seconds
            cursor = connection.cursor()
            cursor.execute("SELECT DB_NAME()")
            row = cursor.fetchone()
        selected_database = str(row[0]) if row else self.config.database
        return f"连接成功 · {self.config.server} / {selected_database}"

    def describe_schema(self) -> str:
        """Return a bounded list of user tables and columns, never business rows."""

        sql = """
SELECT TOP 1000
    s.name AS schema_name,
    t.name AS table_name,
    c.column_id,
    c.name AS column_name,
    ty.name AS type_name,
    c.max_length,
    c.is_nullable
FROM sys.tables AS t
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
JOIN sys.columns AS c ON c.object_id = t.object_id
JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, c.column_id
""".strip()
        try:
            with closing(self._connect()) as connection:
                connection.timeout = self.query_timeout_seconds
                cursor = connection.cursor()
                rows = cursor.execute(sql).fetchall()
        except pyodbc.Error as exc:
            raise ReadOnlyQueryError(self._safe_error("Schema inspection failed", exc)) from exc

        tables: dict[tuple[str, str], list[str]] = {}
        for schema_name, table_name, _column_id, column_name, type_name, length, nullable in rows:
            key = (str(schema_name), str(table_name))
            if key not in tables and len(tables) >= 100:
                break
            columns = tables.setdefault(key, [])
            if len(columns) < 100:
                nullability = " NULL" if bool(nullable) else ""
                size = f"({length})" if isinstance(length, int) and length > 0 else ""
                columns.append(f"{column_name} {type_name}{size}{nullability}")
        descriptions = [
            f"{schema}.{table}({', '.join(columns)})"
            for (schema, table), columns in tables.items()
        ]
        rendered = "\n".join(descriptions) or "No user tables were found."
        return f"Database dialect: Microsoft SQL Server (T-SQL).\n{rendered}"

    def execute(self, query: DataQuery) -> QueryResult:
        validate_read_only_sql(query.sql)
        sql, values = _bind_named_parameters(query.sql, query.parameters)
        row_limit = min(query.max_rows, self.max_rows)
        try:
            with closing(self._connect()) as connection:
                connection.timeout = self.query_timeout_seconds
                cursor = connection.cursor()
                cursor.execute(sql, *values)
                raw_rows = cursor.fetchmany(row_limit + 1)
                columns = _unique_column_names(
                    [str(item[0]) for item in (cursor.description or [])]
                )
        except pyodbc.Error as exc:
            raise ReadOnlyQueryError(self._safe_error("Read-only query failed", exc)) from exc

        sensitive = sensitive_columns(columns)
        rows = [
            {
                column: "[REDACTED]" if column in sensitive else safe_value(value)
                for column, value in zip(columns, row, strict=True)
            }
            for row in raw_rows[:row_limit]
        ]
        return QueryResult(
            query_name=query.name,
            columns=columns,
            rows=rows,
            returned_rows=len(rows),
            truncated=len(raw_rows) > row_limit,
            redacted_columns=sensitive,
        )

    def _connect(self) -> Any:
        try:
            return self._connector(
                self._connection_string(),
                autocommit=True,
                timeout=self.config.connection_timeout_seconds,
            )
        except pyodbc.Error as exc:
            raise ReadOnlyQueryError(self._safe_error("SQL Server connection failed", exc)) from exc

    def _connection_string(self) -> str:
        server = f"tcp:{self.config.server},{self.config.port}"
        parts = [
            f"DRIVER={_odbc_value(self.config.driver)}",
            f"SERVER={_odbc_value(server)}",
            f"DATABASE={_odbc_value(self.config.database)}",
            f"Encrypt={'yes' if self.config.encrypt else 'no'}",
            "TrustServerCertificate="
            + ("yes" if self.config.trust_server_certificate else "no"),
            "ApplicationIntent=ReadOnly",
        ]
        if self.config.authentication == SQLServerAuthentication.WINDOWS:
            parts.append("Trusted_Connection=yes")
        else:
            parts.extend(
                [
                    f"UID={_odbc_value(self.config.username or '')}",
                    f"PWD={_odbc_value(self._password or '')}",
                ]
            )
        return ";".join(parts)

    def _safe_error(self, prefix: str, error: Exception) -> str:
        detail = " ".join(str(error).split())
        if self._password:
            detail = detail.replace(self._password, "[REDACTED]")
        detail = re.sub(r"(?i)(PWD|password)\s*=\s*[^;\s]+", r"\1=[REDACTED]", detail)
        return f"{prefix}: {detail[:800]}"


def _odbc_value(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def _bind_named_parameters(
    sql: str,
    parameters: dict[str, str | int | float | bool | None],
) -> tuple[str, list[str | int | float | bool | None]]:
    values: list[str | int | float | bool | None] = []
    used: set[str] = set()
    rendered: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(sql):
        character = sql[index]
        if quote is not None:
            rendered.append(character)
            if quote == "]" and character == "]":
                if index + 1 < len(sql) and sql[index + 1] == "]":
                    rendered.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            elif quote in {"'", '"'} and character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    rendered.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in {"'", '"', "["}:
            quote = "]" if character == "[" else character
            rendered.append(character)
            index += 1
            continue
        if character in {":", "@"} and index + 1 < len(sql):
            # Preserve SQL Server system variables. Exact structured parameters are still
            # converted to pyodbc `?`, so values are never interpolated into SQL text.
            if character == "@" and sql[index + 1] == "@":
                rendered.append("@@")
                index += 2
                continue
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", sql[index + 1 :])
            if match:
                name = match.group(0)
                if name not in parameters:
                    raise ReadOnlyQueryError(f"Missing query parameter: {name}")
                used.add(name)
                values.append(parameters[name])
                rendered.append("?")
                index += len(name) + 1
                continue
        rendered.append(character)
        index += 1
    unused = set(parameters) - used
    if unused:
        raise ReadOnlyQueryError(f"Unused query parameters: {', '.join(sorted(unused))}")
    return "".join(rendered), values


def _unique_column_names(columns: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    unique: list[str] = []
    for column in columns:
        count = counts.get(column, 0) + 1
        counts[column] = count
        unique.append(column if count == 1 else f"{column}_{count}")
    return unique
