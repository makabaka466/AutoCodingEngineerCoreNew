from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autocoding_agent.adapters.sqlite_database import SQLiteDatabaseReader
from autocoding_agent.config import Settings
from autocoding_agent.database_models import DataQuery


def test_database_settings_default_to_100_rows_and_60_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTO_CODING_DATABASE_MAX_ROWS", raising=False)
    monkeypatch.delenv("AUTO_CODING_DATABASE_QUERY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AUTO_CODING_INCIDENT_MAX_PAGE_QUERY_ROUNDS", raising=False)
    monkeypatch.delenv("AUTO_CODING_INCIDENT_MAX_BUSINESS_QUERY_ROUNDS", raising=False)
    monkeypatch.delenv("AUTO_CODING_INCIDENT_MAX_QUERY_REPAIR_ROUNDS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.database_max_rows == 100
    assert settings.database_query_timeout_seconds == 60
    assert settings.incident_max_page_query_rounds == 2
    assert settings.incident_max_business_query_rounds == 2
    assert settings.incident_max_query_repair_rounds == 1


def test_sqlite_reader_enforces_shared_default_row_limit(tmp_path: Path) -> None:
    database_path = tmp_path / "sample.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO sample (id) VALUES (?)",
            [(index,) for index in range(1, 106)],
        )

    reader = SQLiteDatabaseReader(database_path)
    result = reader.execute(
        DataQuery(
            name="sample_rows",
            purpose="Verify the shared default result boundary.",
            sql="SELECT id FROM sample ORDER BY id",
        )
    )

    assert reader.max_rows == 100
    assert reader.query_timeout_seconds == 60
    assert result.returned_rows == 100
    assert result.truncated is True
