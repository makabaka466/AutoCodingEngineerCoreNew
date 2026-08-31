from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autocoding_agent.adapters.database_safety import ReadOnlyQueryError
from autocoding_agent.adapters.sqlserver_database import (
    SQLServerDatabaseReader,
    available_sqlserver_drivers,
)
from autocoding_agent.incident.models import DataQuery
from autocoding_agent.sqlserver_config import (
    SQLServerAuthentication,
    SQLServerConfigError,
    SQLServerConfigStore,
    SQLServerConnectionConfig,
)


class FakeSecrets:
    def __init__(self, password: str | None = None) -> None:
        self.password = password

    def get(self) -> str | None:
        return self.password

    def set(self, password: str) -> None:
        self.password = password

    def delete(self) -> None:
        self.password = None


class FakeCursor:
    __slots__ = ("description", "executions", "fetch_counts")

    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_counts: list[int] = []
        self.description = [("id",), ("email",), ("auth_token",)]

    def execute(self, sql: str, *parameters: object) -> FakeCursor:
        self.executions.append((sql, parameters))
        return self

    def fetchmany(self, count: int) -> list[tuple[object, ...]]:
        self.fetch_counts.append(count)
        return [
            (1, "one@example.com", "secret-one"),
            (2, "two@example.com", "secret-two"),
            (3, "three@example.com", "secret-three"),
        ][:count]

    def fetchone(self) -> tuple[str]:
        return ("orders",)


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_value = FakeCursor()
        self.closed = False
        self.timeout = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def close(self) -> None:
        self.closed = True


def _config(**overrides: Any) -> SQLServerConnectionConfig:
    values: dict[str, Any] = {
        "server": "sql.internal",
        "port": 1433,
        "database": "orders",
        "driver": "ODBC Driver 17 for SQL Server",
        "authentication": SQLServerAuthentication.WINDOWS,
        "encrypt": True,
    }
    values.update(overrides)
    return SQLServerConnectionConfig(**values)


def test_sqlserver_config_store_keeps_password_out_of_json(tmp_path: Path) -> None:
    secrets = FakeSecrets()
    store = SQLServerConfigStore(tmp_path, secrets)
    config = _config(
        authentication=SQLServerAuthentication.SQL_PASSWORD,
        username="incident_reader",
    )

    state = store.save(config, "database-secret")

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert state.configured is True
    assert state.has_password is True
    assert secrets.password == "database-secret"
    assert "password" not in document
    assert "database-secret" not in store.path.read_text(encoding="utf-8")


def test_sqlserver_config_store_preserves_existing_password_when_blank(
    tmp_path: Path,
) -> None:
    secrets = FakeSecrets("existing-secret")
    store = SQLServerConfigStore(tmp_path, secrets)
    config = _config(
        authentication=SQLServerAuthentication.SQL_PASSWORD,
        username="incident_reader",
    )

    store.save(config, "")

    assert secrets.password == "existing-secret"


def test_sqlserver_config_rolls_back_password_when_json_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = FakeSecrets()
    store = SQLServerConfigStore(tmp_path, secrets)
    original = _config(
        authentication=SQLServerAuthentication.SQL_PASSWORD,
        username="old_reader",
    )
    store.save(original, "old-secret")
    original_json = store.path.read_text(encoding="utf-8")
    replacement = _config(
        database="new_orders",
        authentication=SQLServerAuthentication.SQL_PASSWORD,
        username="new_reader",
    )
    monkeypatch.setattr(
        "autocoding_agent.sqlserver_config.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        store.save(replacement, "new-secret")

    assert secrets.password == "old-secret"
    assert store.path.read_text(encoding="utf-8") == original_json


def test_sqlserver_config_requires_credentials_for_sql_auth(tmp_path: Path) -> None:
    store = SQLServerConfigStore(tmp_path, FakeSecrets())
    config = _config(authentication=SQLServerAuthentication.SQL_PASSWORD)

    with pytest.raises(SQLServerConfigError, match="用户名"):
        store.save(config, "secret")


def test_sqlserver_reader_binds_parameters_bounds_rows_and_redacts() -> None:
    connection = FakeConnection()
    captured: dict[str, object] = {}

    def connector(connection_string: str, **kwargs: object) -> FakeConnection:
        captured["connection_string"] = connection_string
        captured.update(kwargs)
        return connection

    reader = SQLServerDatabaseReader(
        _config(),
        max_rows=2,
        query_timeout_seconds=7,
        connector=connector,
    )

    result = reader.execute(
        DataQuery(
            name="order_users",
            purpose="Inspect affected users.",
            sql="SELECT id, email, auth_token FROM dbo.users WHERE id >= :minimum_id",
            parameters={"minimum_id": 1},
            max_rows=10,
        )
    )

    assert "ApplicationIntent=ReadOnly" in str(captured["connection_string"])
    assert "Trusted_Connection=yes" in str(captured["connection_string"])
    assert captured["autocommit"] is True
    assert connection.cursor_value.executions == [
        (
            "SELECT id, email, auth_token FROM dbo.users WHERE id >= ?",
            (1,),
        )
    ]
    assert connection.timeout == 7
    assert result.returned_rows == 2
    assert result.truncated is True
    assert result.redacted_columns == ["auth_token"]
    assert result.rows[0]["auth_token"] == "[REDACTED]"


def test_sqlserver_reader_defaults_to_60_seconds_and_100_rows() -> None:
    connection = FakeConnection()
    reader = SQLServerDatabaseReader(
        _config(),
        connector=lambda *_args, **_kwargs: connection,
    )
    query = DataQuery(
        name="bounded_default",
        purpose="Use the shared default result boundary.",
        sql="SELECT id, email, auth_token FROM dbo.users",
    )

    reader.execute(query)

    assert query.max_rows == 100
    assert reader.max_rows == 100
    assert connection.timeout == 60
    assert connection.cursor_value.fetch_counts == [101]


def test_sqlserver_parameter_binding_ignores_colons_inside_literals() -> None:
    connection = FakeConnection()
    reader = SQLServerDatabaseReader(
        _config(),
        connector=lambda *_args, **_kwargs: connection,
    )

    reader.execute(
        DataQuery(
            name="time_format",
            purpose="Keep the format literal intact.",
            sql="SELECT FORMAT(created_at, 'HH:mm') FROM dbo.orders WHERE id = :id",
            parameters={"id": 42},
        )
    )

    assert connection.cursor_value.executions[0] == (
        "SELECT FORMAT(created_at, 'HH:mm') FROM dbo.orders WHERE id = ?",
        (42,),
    )


def test_sqlserver_reader_safely_accepts_colon_and_at_named_parameters() -> None:
    connection = FakeConnection()
    reader = SQLServerDatabaseReader(
        _config(),
        connector=lambda *_args, **_kwargs: connection,
    )

    reader.execute(
        DataQuery(
            name="compatible_parameters",
            purpose="Accept SQL Server-style model output without interpolating values.",
            sql=(
                "SELECT @@ROWCOUNT, id FROM dbo.orders "
                "WHERE id >= @minimum_id AND id <= :maximum_id "
                "AND note = '@minimum_id'"
            ),
            parameters={"minimum_id": 1, "maximum_id": 9},
        )
    )

    assert connection.cursor_value.executions[0] == (
        "SELECT @@ROWCOUNT, id FROM dbo.orders "
        "WHERE id >= ? AND id <= ? AND note = '@minimum_id'",
        (1, 9),
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE dbo.orders SET status = 'done'",
        "SELECT id FROM dbo.orders; DELETE FROM dbo.orders",
        "SELECT id INTO dbo.copy FROM dbo.orders",
        "SELECT id FROM dbo.orders -- unsafe comment",
    ],
)
def test_sqlserver_reader_rejects_non_read_only_sql(sql: str) -> None:
    reader = SQLServerDatabaseReader(_config(), connector=lambda *_args, **_kwargs: None)

    with pytest.raises(ReadOnlyQueryError):
        reader.execute(DataQuery(name="unsafe", purpose="Reject it.", sql=sql))


def test_sqlserver_drivers_prefer_modern_odbc_driver() -> None:
    drivers = available_sqlserver_drivers(
        lambda: ["SQL Server", "ODBC Driver 17 for SQL Server", "Other Driver"]
    )

    assert drivers == ["ODBC Driver 17 for SQL Server", "SQL Server"]
