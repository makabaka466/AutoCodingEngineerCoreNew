"""项目隔离、来源审计、配置漂移和旧任务兼容的行为验证。"""

from types import SimpleNamespace

import pytest
from test_agent_flows import ScriptedRuntime
from test_incident_flow import ScriptedStructuredRuntime

from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision, AgentSession, AgentStatus
from autocoding_agent.database_models import DataQuery, QueryResult
from autocoding_agent.datasource_models import DataSourceDefinition, ProjectDataSources
from autocoding_agent.datasources import DataSourceService, TaskDatabaseAccess
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import IncidentDecision, IncidentQueryStage, IncidentStatus
from autocoding_agent.sqlserver_config import (
    KeyringSecretStore,
    SQLServerAuthentication,
    SQLServerConnectionConfig,
)
from autocoding_agent.sqlserver_service import SQLServerConnectionService


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    credentials = {}
    calls = []
    monkeypatch.setattr(KeyringSecretStore, "get", lambda self: credentials.get(self.account))
    monkeypatch.setattr(
        KeyringSecretStore, "set", lambda self, value: credentials.update({self.account: value})
    )
    monkeypatch.setattr(
        KeyringSecretStore, "delete", lambda self: credentials.pop(self.account, None)
    )
    monkeypatch.setattr(SQLServerConnectionService, "_validate_driver", lambda *args: None)

    class Reader:
        def __init__(self, config):
            self.config = config

        def execute(self, query):
            calls.append((self.config.database, query.datasource_id))
            return QueryResult(
                query_name=query.name, columns=["id"], rows=[{"id": 1}], returned_rows=1
            )

    monkeypatch.setattr(
        SQLServerConnectionService, "_reader", lambda self, config, password: Reader(config)
    )
    settings = Settings(data_dir=tmp_path / "state", rag_enabled=False)
    service = DataSourceService(settings)
    for key, env in [("ds-menu", "test"), ("ds-business", "production"), ("ds-other", "test")]:
        service.save_source(
            DataSourceDefinition(datasource_id=key, name=key, environment=env, schema_name="dbo"),
            SQLServerConnectionConfig(server="example", database=key),
        )
    service.bind_project(
        "project-a",
        ProjectDataSources(
            source_ids=["ds-menu", "ds-business"],
            menu_default="ds-menu",
            business_default="ds-business",
        ),
    )
    service.bind_project("project-b", ProjectDataSources(source_ids=["ds-other"]))
    return service, calls, credentials


def query(source=None):
    return DataQuery(
        name="lookup", purpose="核对数据", sql="SELECT id FROM dbo.orders", datasource_id=source
    )


def test_defaults_and_explicit_sources_are_routed_with_provenance(catalog):
    service, calls, _ = catalog
    snapshot = service.snapshot("project-a")
    menu = service.execute(snapshot, query(), "page_lookup")
    business = service.execute(snapshot, query(), "business_data")
    service.execute(snapshot, query("ds-menu"), "business_data")
    assert calls == [("ds-menu", "ds-menu"), ("ds-business", "ds-business"), ("ds-menu", "ds-menu")]
    assert menu.source.environment == "test"
    assert business.source.environment == "production"
    assert business.source.schema_name == "dbo"


def test_cross_project_selection_never_executes(catalog):
    service, calls, _ = catalog
    with pytest.raises(ValueError, match="不属于"):
        service.execute(service.snapshot("project-a"), query("ds-other"), None)
    assert calls == []


def test_ambiguous_sources_do_not_choose_first(catalog):
    service, calls, _ = catalog
    service.bind_project("project-a", ProjectDataSources(source_ids=["ds-menu", "ds-business"]))
    with pytest.raises(ValueError, match="不明确"):
        service.execute(service.snapshot("project-a"), query(), None)
    assert calls == []


@pytest.mark.parametrize("change", ["environment", "connection", "binding", "default"])
def test_configuration_drift_blocks_restored_task(catalog, change):
    service, calls, _ = catalog
    snapshot = service.snapshot("project-a")
    if change == "binding":
        service.bind_project("project-a", ProjectDataSources(source_ids=["ds-menu"]))
    elif change == "default":
        service.bind_project(
            "project-a",
            ProjectDataSources(
                source_ids=["ds-menu", "ds-business"],
                menu_default="ds-business",
                business_default="ds-business",
            ),
        )
    else:
        definition = service.load().sources["ds-business"]
        if change == "environment":
            definition.environment = "different"
        service.save_source(
            definition,
            SQLServerConnectionConfig(
                server="changed" if change == "connection" else "example",
                database="ds-business",
            ),
        )
    with pytest.raises(ValueError, match="变化"):
        service.execute(snapshot, query("ds-business"), None)
    assert calls == []


def test_new_unbound_task_cannot_fall_back_to_legacy(catalog):
    service, calls, _ = catalog
    access = TaskDatabaseAccess(service, SimpleNamespace(execute=lambda q: calls.append(q)), "old")
    session = AgentSession(workspace=".", goal="test", project="unbound", database_reference="old")
    access.bind(session)
    assert session.database_binding is not None
    assert not access.available(session)
    with pytest.raises(ValueError):
        access.execute(session, query())
    assert calls == []


def test_old_task_keeps_legacy_connection_after_catalog_enabled(catalog):
    service, _, _ = catalog
    access = TaskDatabaseAccess(service, SimpleNamespace(execute=lambda q: "legacy"), "old")
    session = AgentSession(workspace=".", goal="test", database_reference="old")
    assert access.execute(session, query()) == "legacy"
    with pytest.raises(ValueError, match="旧单连接"):
        access.execute(session, query("ds-menu"))
    session.database_reference = "different"
    with pytest.raises(ValueError, match="变化"):
        access.execute(session, query())


def test_named_passwords_are_isolated_and_not_in_snapshot(catalog):
    service, _, credentials = catalog
    for key, password in [("ds-menu", "secret-menu"), ("ds-business", "secret-business")]:
        service.save_source(
            service.load().sources[key],
            SQLServerConnectionConfig(
                server="example",
                database=key,
                authentication=SQLServerAuthentication.SQL_PASSWORD,
                username="reader",
            ),
            password,
        )
    assert credentials["ds-menu"] != credentials["ds-business"]
    assert "secret-" not in service.snapshot("project-a").model_dump_json()
    assert "secret-" not in service.path.read_text(encoding="utf-8")
    for path in service.path.parent.joinpath("connections").glob("*.json"):
        assert "secret-" not in path.read_text(encoding="utf-8")


def test_binding_rejects_unknown_source_and_unbound_default(catalog):
    service, _, _ = catalog
    with pytest.raises(ValueError):
        service.bind_project("project-a", ProjectDataSources(source_ids=["ds-unknown"]))
    with pytest.raises(ValueError):
        ProjectDataSources(source_ids=["ds-menu"], business_default="ds-other")
    assert service.load().projects["project-a"].business_default == "ds-business"


def test_development_routes_persists_and_returns_evidence(catalog, tmp_path):
    service, calls, _ = catalog
    runtime = ScriptedRuntime(
        AgentDecision(status=AgentStatus.QUERY_REQUIRED, message="核对", queries=[query()]),
        AgentDecision(
            status=AgentStatus.NEEDS_INPUT, message="请提供业务目标", reason="需要明确范围"
        ),
    )
    app = build_application(service.settings, runtime=runtime)
    app._engine.capabilities.knowledge_root = None
    outcome = app.start(tmp_path, "分析订单状态", "project-a")
    session = app.get_session(outcome.session_id)
    assert calls == [("ds-business", "ds-business")]
    assert session.database_binding.project == "project-a"
    assert session.query_observations[0].source.environment == "production"
    assert "production" in runtime.turns[0].system_prompt
    assert "datasource_id" in runtime.turns[1].user_message
    assert '"server":' not in session.database_binding.model_dump_json()


def test_incident_menu_query_uses_project_default(catalog, tmp_path):
    service, calls, _ = catalog
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="定位页面",
                query_stage=IncidentQueryStage.PAGE_LOOKUP,
                queries=[query()],
            ),
            IncidentDecision(
                status=IncidentStatus.NEEDS_INPUT, message="请确认页面", question="是订单页吗？"
            ),
        ]
    )
    app = build_incident_application(service.settings, runtime=runtime)
    app._engine.capabilities.knowledge_root = None
    outcome = app.start(tmp_path, "订单页面异常", project="project-a")
    session = app.get_session(outcome.session_id)
    assert calls == [("ds-menu", "ds-menu")]
    assert session.query_observations[0].source.datasource_id == "ds-menu"
    assert session.database_binding.project == "project-a"


def test_one_application_handles_two_projects_without_switching_global_reader(catalog, tmp_path):
    service, calls, _ = catalog
    runtime = ScriptedRuntime(*[
        decision for _ in range(2) for decision in [
            AgentDecision(status=AgentStatus.QUERY_REQUIRED, message="核对", queries=[query()]),
            AgentDecision(status=AgentStatus.NEEDS_INPUT, message="请补充目标"),
        ]
    ])
    app = build_application(service.settings, runtime=runtime)
    app._engine.capabilities.knowledge_root = None
    app.start(tmp_path, "核对业务数据", "project-a")
    app.start(tmp_path, "核对业务数据", "project-b")
    assert calls == [("ds-business", "ds-business"), ("ds-other", "ds-other")]


def test_development_partial_batch_failure_keeps_successful_source(catalog, tmp_path):
    service, calls, _ = catalog
    runtime = ScriptedRuntime(AgentDecision(
        status=AgentStatus.QUERY_REQUIRED, message="核对",
        queries=[query("ds-menu"), query("ds-other")],
    ))
    app = build_application(service.settings, runtime=runtime)
    app._engine.capabilities.knowledge_root = None
    outcome = app.start(tmp_path, "核对业务数据", "project-a")
    session = app.get_session(outcome.session_id)
    assert outcome.status == AgentStatus.FAILED
    assert calls == [("ds-menu", "ds-menu")]
    assert len(session.query_observations) == 2
    assert session.query_observations[0].source.datasource_id == "ds-menu"
    assert session.query_observations[1].status == "failed"
    assert session.query_observations[1].datasource_id == "ds-other"


def test_restart_reads_snapshot_and_blocks_changed_connection(catalog, tmp_path):
    service, calls, _ = catalog
    app = build_application(service.settings, runtime=ScriptedRuntime(
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="请补充订单号"),
    ))
    app._engine.capabilities.knowledge_root = None
    outcome = app.start(tmp_path, "核对业务数据", "project-a")
    service.save_source(service.load().sources["ds-business"], SQLServerConnectionConfig(
        server="another-server", database="ds-business",
    ))
    restored = build_application(service.settings, runtime=ScriptedRuntime(
        AgentDecision(status=AgentStatus.QUERY_REQUIRED, message="核对", queries=[query()]),
    ))
    restored._engine.capabilities.knowledge_root = None
    result = restored.send(outcome.session_id, "订单号 123")
    assert result.status == AgentStatus.FAILED
    assert "变化" in result.message
    assert calls == []


def test_connection_is_not_reloaded_between_validation_and_execution(catalog, monkeypatch):
    service, calls, _ = catalog
    snapshot = service.snapshot("project-a")
    original = service._configuration

    def change_after_read(source, expected):
        result = original(source, expected)
        service.save_source(source, SQLServerConnectionConfig(server="new", database="new-db"))
        return result

    monkeypatch.setattr(service, "_configuration", change_after_read)
    service.execute(snapshot, query("ds-business"), None)
    assert calls == [("ds-business", "ds-business")]
    monkeypatch.setattr(service, "_configuration", original)
    with pytest.raises(ValueError, match="变化"):
        service.execute(snapshot, query("ds-business"), None)


def test_interrupted_source_save_cannot_create_mixed_environment_snapshot(catalog, monkeypatch):
    service, calls, _ = catalog
    original = service._save

    def fail_write(_catalog):
        raise OSError("simulated interrupted catalog write")

    monkeypatch.setattr(service, "_save", fail_write)
    definition = service.load().sources["ds-menu"]
    definition.environment = "production"
    with pytest.raises(OSError):
        service.save_source(definition, SQLServerConnectionConfig(server="prod", database="prod"))
    with pytest.raises(ValueError, match="不一致"):
        service.snapshot("project-a")
    assert calls == []
    monkeypatch.setattr(service, "_save", original)
    service.save_source(definition, SQLServerConnectionConfig(server="prod", database="prod"))
    assert service.snapshot("project-a").sources["ds-menu"].environment == "production"
