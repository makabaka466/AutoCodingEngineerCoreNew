"""Tests for the compact database capability prompt."""

from autocoding_agent.database_prompt import compact_database_context


def test_compact_database_context_describes_sql_server_without_catalog() -> None:
    context = compact_database_context(
        configured=True,
        reference="sqlserver://sql.internal:1433/QTMES",
    )

    assert "Microsoft SQL Server (T-SQL)" in context
    assert "INFORMATION_SCHEMA.COLUMNS" in context
    assert "sql.internal" not in context
    assert "QTMES" not in context


def test_compact_database_context_describes_sqlite_metadata() -> None:
    context = compact_database_context(configured=True, reference="state/agent.db")

    assert "SQLite" in context
    assert "sqlite_master" in context


def test_compact_database_context_reports_unconfigured_reader() -> None:
    assert "No shared read-only database is configured" in compact_database_context(
        configured=False,
        reference=None,
    )
