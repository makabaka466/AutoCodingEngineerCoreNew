"""Compact database capability context shared by both ACE workflows."""

from __future__ import annotations


def compact_database_context(*, configured: bool, reference: str | None) -> str:
    """Describe SQL capabilities without injecting the full catalog every turn.

    The model can still inspect schema through bounded read-only metadata queries after
    source code identifies likely tables. This keeps unrelated schemas out of every prompt.
    """

    if not configured:
        return "No shared read-only database is configured for this task."
    normalized = (reference or "").casefold()
    if normalized.startswith("sqlserver://"):
        dialect = "Microsoft SQL Server (T-SQL)"
        metadata = "INFORMATION_SCHEMA.COLUMNS or bounded sys.tables/sys.columns SELECTs"
    elif normalized.endswith((".db", ".sqlite", ".sqlite3")):
        dialect = "SQLite"
        metadata = "sqlite_master and bounded PRAGMA table_info reads"
    else:
        dialect = "the configured read-only SQL connection"
        metadata = "a minimal dialect-appropriate metadata query"
    return (
        f"A {dialect} database is configured. The full catalog is intentionally omitted to "
        "avoid repeatedly sending unrelated tables. Derive target tables and columns from the "
        "verified source code. If schema details are still needed, include "
        f"{metadata} in the same bounded query batch as the related evidence query when safe. "
        "The host still validates every query as parameterized and read-only."
    )
