"""Shared database read-only validation and result sanitization."""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_COLUMN = re.compile(
    r"(?:password|passwd|secret|token|api.?key|authorization|credential|cookie|session)",
    re.IGNORECASE,
)
_BLOCKED_SQL = re.compile(
    r"\b(?:insert|update|delete|merge|execute?|create|alter|drop|truncate|grant|revoke|"
    r"deny|backup|restore|dbcc|use|set|into|openrowset|opendatasource|openquery|bulk|"
    r"waitfor|shutdown|kill)\b",
    re.IGNORECASE,
)


class ReadOnlyQueryError(ValueError):
    """Raised when a proposed query violates the database read boundary."""


def validate_read_only_sql(sql: str, *, allow_explain: bool = False) -> None:
    """Reject multi-statement, commented, and potentially mutating SQL."""

    prefix = r"^\s*(?:select|with|explain)\b" if allow_explain else r"^\s*(?:select|with)\b"
    if not re.match(prefix, sql, re.IGNORECASE):
        allowed = "SELECT, WITH, or EXPLAIN" if allow_explain else "SELECT or WITH"
        raise ReadOnlyQueryError(f"Only {allowed} queries are accepted.")
    if "\x00" in sql:
        raise ReadOnlyQueryError("Query contains an invalid null byte.")
    if ";" in sql:
        raise ReadOnlyQueryError("Multiple statements and semicolons are not accepted.")
    if "--" in sql or "/*" in sql or "*/" in sql:
        raise ReadOnlyQueryError("SQL comments are not accepted in model-proposed queries.")
    searchable = _without_literals_and_quoted_identifiers(sql)
    blocked = _BLOCKED_SQL.search(searchable)
    if blocked:
        raise ReadOnlyQueryError(
            f"Read-only query contains a blocked SQL operation: {blocked.group(0).upper()}."
        )


def sensitive_columns(columns: list[str]) -> list[str]:
    return [column for column in columns if _SENSITIVE_COLUMN.search(column)]


def safe_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return f"[BINARY {len(value)} bytes]"
    rendered = str(value)
    return rendered if len(rendered) <= 500 else f"{rendered[:500]}…"


def _without_literals_and_quoted_identifiers(sql: str) -> str:
    without_strings = re.sub(r"'(?:''|[^'])*'", "''", sql)
    without_brackets = re.sub(r"\[(?:\]\]|[^]])*]", "[]", without_strings)
    return re.sub(r'"(?:""|[^"])*"', '""', without_brackets)
