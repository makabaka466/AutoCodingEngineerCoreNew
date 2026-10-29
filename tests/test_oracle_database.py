"""Oracle 适配器与命名数据源的离线行为测试，不连接业务数据库。"""

from types import SimpleNamespace

import oracledb
import pytest
from test_agent_flows import ScriptedRuntime
from test_datasources import catalog as catalog
from test_incident_flow import ScriptedStructuredRuntime

from autocoding_agent.adapters import oracle_database as adapter
from autocoding_agent.adapters.database_safety import ReadOnlyQueryError
from autocoding_agent.application import build_application
from autocoding_agent.core.models import AgentDecision, AgentSession, AgentStatus
from autocoding_agent.database_models import DataQuery, QueryResult
from autocoding_agent.datasource_models import DataSourceDefinition, ProjectDataSources
from autocoding_agent.datasources import TaskDatabaseAccess
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import IncidentDecision, IncidentQueryStage, IncidentStatus
from autocoding_agent.oracle_config import OracleConnectionConfig
from autocoding_agent.oracle_service import OracleConnectionService


class Cursor:
    def __init__(self, rows=None, names=None, binds=None):
        self.rows = rows if rows is not None else [(1,)]
        self.description = [(name,) for name in (names or ["ID"])]
        self.binds = binds or []
        self.calls = []
        self.closed = False

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))

    def prepare(self, sql):
        self.prepared = sql

    def bindnames(self):
        return self.binds

    def fetchmany(self, count):
        self.fetch_count = count
        return self.rows[:count]

    def fetchone(self):
        return self.rows[0]

    def close(self):
        self.closed = True


class Connection:
    version = "19.15.0.0.0"

    def __init__(self, cursor):
        self.result_cursor = cursor
        self.closed = False

    def cursor(self):
        return self.result_cursor

    def close(self):
        self.closed = True


def config(**kwargs):
    return OracleConnectionConfig(
        server="oracle.example", database="MES", username="reader", **kwargs
    )


def query(**kwargs):
    return DataQuery(
        name="evidence",
        purpose="核对业务数据",
        sql=kwargs.pop("sql", "SELECT id FROM orders"),
        **kwargs,
    )


def test_binding_limit_redaction_and_resources(monkeypatch):
    cursor = Cursor([(1, "secret"), (2, "hidden"), (3, "extra")], ["ID", "PASSWORD"], ["ID"])
    connection = Connection(cursor)
    reader = adapter.OracleDatabaseReader(config(), "password", max_rows=2, query_timeout_seconds=7)
    monkeypatch.setattr(reader, "_connect", lambda: connection)
    result = reader.execute(
        query(sql="SELECT id, password FROM orders WHERE id=:id OR id=:id", parameters={"id": 1})
    )
    assert cursor.calls == [("SET TRANSACTION READ ONLY", None), (None, {"ID": 1})]
    assert cursor.fetch_count == 3 and cursor.arraysize == 3 and cursor.prefetchrows == 3
    assert result.returned_rows == 2 and result.truncated
    assert result.rows[0] == {"ID": 1, "PASSWORD": "[REDACTED]"}
    assert connection.call_timeout == 7000 and connection.autocommit is False
    assert cursor.closed and connection.closed


def test_duplicate_aliases_preserve_every_column(monkeypatch):
    cursor = Cursor([(1, 2, 3)], ["ID", "ID", "ID_2"])
    reader = adapter.OracleDatabaseReader(config(), "password")
    monkeypatch.setattr(reader, "_connect", lambda: Connection(cursor))
    result = reader.execute(query())
    assert len(result.columns) == len(set(result.columns)) == 3
    assert list(result.rows[0].values()) == [1, 2, 3]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET state=1",
        "DELETE FROM orders",
        "BEGIN NULL; END;",
        "SELECT id FROM orders FOR UPDATE",
        "SELECT order_seq.NEXTVAL FROM dual",
        "SELECT dbms_random.value FROM dual",
        "SELECT id FROM orders@remote",
        "SELECT id FROM orders; DELETE FROM orders",
        "SELECT /* hint */ id FROM orders",
    ],
)
def test_unsafe_sql_rejected_before_connection(monkeypatch, sql):
    reader = adapter.OracleDatabaseReader(config(), "password")
    monkeypatch.setattr(reader, "_connect", lambda: pytest.fail("must not connect"))
    with pytest.raises(ReadOnlyQueryError):
        reader.execute(query(sql=sql))


def test_mismatched_parameters_never_execute_user_sql(monkeypatch):
    cursor = Cursor(binds=["ID"])
    connection = Connection(cursor)
    reader = adapter.OracleDatabaseReader(config(), "password")
    monkeypatch.setattr(reader, "_connect", lambda: connection)
    with pytest.raises(ReadOnlyQueryError, match="不一致"):
        reader.execute(query(sql="SELECT :id FROM dual", parameters={"other": 3}))
    assert cursor.calls == [("SET TRANSACTION READ ONLY", None)]
    assert cursor.closed and connection.closed


def test_oracle_boolean_is_not_silently_mapped_to_unknown_business_flag(monkeypatch):
    reader = adapter.OracleDatabaseReader(config(), "password")
    monkeypatch.setattr(reader, "_connect", lambda: pytest.fail("must not connect"))
    with pytest.raises(ReadOnlyQueryError, match="实际数值或字符"):
        reader.execute(
            query(sql="SELECT id FROM orders WHERE flag=:flag", parameters={"flag": True})
        )


def test_lob_is_bounded_and_sensitive_lob_is_not_read(monkeypatch):
    class Lob:
        type = oracledb.DB_TYPE_CLOB

        def __init__(self):
            self.reads = []

        def read(self, offset, amount):
            self.reads.append((offset, amount))
            return "中" * amount

    text, secret = Lob(), Lob()
    monkeypatch.setattr(adapter.oracledb, "LOB", Lob)
    cursor = Cursor([(text, secret)], ["DETAIL", "TOKEN"])
    reader = adapter.OracleDatabaseReader(config(), "password")
    monkeypatch.setattr(reader, "_connect", lambda: Connection(cursor))
    result = reader.execute(query())
    assert text.reads == [(1, 501)] and secret.reads == []
    assert len(result.rows[0]["DETAIL"]) == 501
    assert result.rows[0]["TOKEN"] == "[REDACTED]"


def test_server_version_test_only_reads_identity(monkeypatch):
    cursor = Cursor([("MES", "MES_SERVICE", "MES_OWNER")])
    connection = Connection(cursor)
    reader = adapter.OracleDatabaseReader(config(), "password")
    monkeypatch.setattr(reader, "_connect", lambda: connection)
    result = reader.test_connection()
    assert "19.15.0.0.0" in result and "MES_OWNER" in result and "thin" in result
    assert "SYS_CONTEXT" in cursor.calls[1][0]
    assert cursor.closed and connection.closed


@pytest.mark.parametrize("identifier", ["service_name", "sid"])
def test_connect_params_are_structured_and_connection_has_no_retries(monkeypatch, identifier):
    monkeypatch.setattr(adapter, "_MODE_SIGNATURE", None)
    monkeypatch.setattr(adapter.oracledb, "enable_thin_mode", lambda: None)
    captured = {}
    monkeypatch.setattr(
        adapter.oracledb, "connect", lambda **kw: captured.update(kw) or "connection"
    )
    reader = adapter.OracleDatabaseReader(config(identifier_type=identifier), "secret")
    assert reader._connect() == "connection"
    descriptor = captured["params"].get_connect_string()
    assert f"({identifier.upper()}=MES)" in descriptor
    assert captured["params"].retry_count == 0
    assert captured["params"].tcp_connect_timeout == 10
    assert captured["password"] == "secret" and "secret" not in descriptor


def test_process_mode_conflict_does_not_connect(monkeypatch):
    monkeypatch.setattr(adapter, "_MODE_SIGNATURE", ("thin", ""))
    monkeypatch.setattr(adapter.oracledb, "connect", lambda **kw: pytest.fail("must not connect"))
    reader = adapter.OracleDatabaseReader(config(mode="thick"), "secret")
    with pytest.raises(ReadOnlyQueryError, match="重启"):
        reader._connect()


def test_thick_initializes_once_and_requires_modern_client(monkeypatch):
    monkeypatch.setattr(adapter, "_MODE_SIGNATURE", None)
    calls = []
    monkeypatch.setattr(adapter.oracledb, "init_oracle_client", lambda **kw: calls.append(kw))
    monkeypatch.setattr(adapter.oracledb, "clientversion", lambda: (19, 3, 0, 0, 0))
    monkeypatch.setattr(adapter.oracledb, "connect", lambda **kw: "connection")
    reader = adapter.OracleDatabaseReader(config(mode="thick"), "secret")
    reader._connect()
    reader._connect()
    assert calls == [{"lib_dir": None}]


def test_driver_errors_do_not_expose_password_sql_or_business_values(monkeypatch):
    reader = adapter.OracleDatabaseReader(config(), "very-secret")
    connection = Connection(Cursor())
    monkeypatch.setattr(reader, "_connect", lambda: connection)

    def fail(*args):
        raise oracledb.DatabaseError(
            "ORA-00942 password=very-secret select patient_secret from private"
        )

    monkeypatch.setattr(connection.result_cursor, "execute", fail)
    with pytest.raises(ReadOnlyQueryError) as error:
        reader.execute(query())
    assert "ORA-00942" in str(error.value)
    assert "very-secret" not in str(error.value) and "patient" not in str(error.value)
    assert connection.closed


def test_oracle_save_binding_and_mixed_dialect_context(catalog, monkeypatch):
    service, calls, _ = catalog
    definition = DataSourceDefinition(
        datasource_id="ds-oracle",
        database_type="oracle",
        name="车载",
        environment="test",
        schema_name="MES",
    )
    service.save_source(definition, config(), "oracle-secret")
    assert service.connection("ds-oracle").inspect().configured
    assert "oracle-secret" not in service.connection("ds-oracle").store.path.read_text(
        encoding="utf-8"
    )
    service.bind_project(
        "mixed",
        ProjectDataSources(
            source_ids=["ds-oracle", "ds-menu"],
            menu_default="ds-oracle",
            business_default="ds-menu",
        ),
    )
    monkeypatch.setattr(
        OracleConnectionService,
        "_reader",
        lambda *args: SimpleNamespace(
            execute=lambda q: QueryResult(
                query_name=q.name, columns=["ID"], rows=[{"ID": 1}], returned_rows=1
            ),
        ),
    )
    access = TaskDatabaseAccess(service, None, None)
    session = AgentSession(workspace=".", goal="核对", project="mixed")
    access.bind(session)
    oracle_result = access.execute(session, query(), "page_lookup")
    sql_result = access.execute(session, query(), "business_data")
    assert oracle_result.source.database_type == "oracle"
    assert sql_result.source.database_type == "sqlserver"
    assert "Oracle SQL" in access.context(session) and "T-SQL" in access.context(session)
    assert calls == [("ds-menu", "ds-menu")]
    with pytest.raises(ValueError, match="不属于"):
        access.execute(session, query(datasource_id="ds-business"))


def test_oracle_configuration_change_blocks_old_snapshot(catalog):
    service, _, _ = catalog
    definition = DataSourceDefinition(
        datasource_id="ds-oracle", database_type="oracle", name="车载", environment="test"
    )
    service.save_source(definition, config(), "secret")
    service.bind_project("oracle", ProjectDataSources(source_ids=["ds-oracle"]))
    snapshot = service.snapshot("oracle")
    service.save_source(definition, config(identifier_type="sid"))
    with pytest.raises(ValueError, match="变化"):
        service.execute(snapshot, query(), None)


@pytest.mark.parametrize("workflow", ["development", "incident"])
def test_oracle_evidence_round_trip_in_both_workflows(catalog, tmp_path, monkeypatch, workflow):
    service, _, _ = catalog
    service.save_source(
        DataSourceDefinition(
            datasource_id="ds-oracle",
            name="车载",
            database_type="oracle",
            environment="test",
        ),
        config(),
        "secret",
    )
    service.bind_project("vehicle", ProjectDataSources(source_ids=["ds-oracle"]))
    monkeypatch.setattr(
        OracleConnectionService,
        "_reader",
        lambda *args: SimpleNamespace(
            execute=lambda q: QueryResult(
                query_name=q.name,
                columns=["ID"],
                rows=[{"ID": 1}],
                returned_rows=1,
            ),
        ),
    )
    if workflow == "development":
        runtime = ScriptedRuntime(
            AgentDecision(status=AgentStatus.QUERY_REQUIRED, message="核对", queries=[query()]),
            AgentDecision(status=AgentStatus.NEEDS_INPUT, message="请补充目标"),
        )
        app = build_application(service.settings, runtime=runtime)
    else:
        runtime = ScriptedStructuredRuntime(
            [
                IncidentDecision(
                    status=IncidentStatus.QUERY_REQUIRED,
                    message="定位",
                    query_stage=IncidentQueryStage.PAGE_LOOKUP,
                    queries=[query()],
                ),
                IncidentDecision(
                    status=IncidentStatus.NEEDS_INPUT, message="请确认页面", question="是车载页吗？"
                ),
            ]
        )
        app = build_incident_application(service.settings, runtime=runtime)
    app._engine.capabilities.knowledge_root = None
    outcome = app.start(tmp_path, "车载页面", project="vehicle")
    session = app.get_session(outcome.session_id)
    assert outcome.status.value == "needs_input"
    assert len(runtime.turns) == 2
    assert session.query_observations[0].source.database_type == "oracle"
    assert session.query_observations[0].source.datasource_id == "ds-oracle"
