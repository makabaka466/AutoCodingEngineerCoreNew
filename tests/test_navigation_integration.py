"""目录请求经共享数据库门面和两套工作流的集成验证。"""

import json
from types import SimpleNamespace

import pytest
from test_agent_flows import ScriptedRuntime, _app
from test_incident_flow import ScriptedStructuredRuntime
from test_navigation_catalog import menu, setup

from autocoding_agent.adapters.json_incident_store import JsonIncidentStore
from autocoding_agent.core.models import AgentDecision
from autocoding_agent.database_models import DataQuery
from autocoding_agent.datasources import TaskDatabaseAccess
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import IncidentDecision
from autocoding_agent.navigation_config import load_navigation_config


def configured(tmp_path):
    _, session, config, fake = setup(tmp_path, [menu(1, path="Client.Order1")])
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/Order1.cs").write_text("class Order1 {}")
    folder = tmp_path / "knowledge/navigation/ace-one"
    folder.mkdir(parents=True)
    payload = {
        "catalogs": [config.model_copy(update={"datasource_id": "menu_default"}).model_dump()],
        "path_mappings": [{"namespace_prefix": "Client", "relative_directory": "src"}],
    }
    (folder / "ace-one.md").write_text(
        "```ace-navigation\n" + json.dumps(payload) + "\n```", encoding="utf-8"
    )

    def execute(snapshot, query, stage):
        return fake.execute(session, query, stage)

    service = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path / "state"),
        snapshot=lambda project: session.database_binding,
        execute=execute,
    )
    return (
        session,
        fake,
        TaskDatabaseAccess(service, None, None, knowledge_root=tmp_path / "knowledge"),
    )


def test_catalog_request_resolves_paths_cache_and_rejects_business_stage(tmp_path):
    session, fake, access = configured(tmp_path)
    query = DataQuery(name="menus", purpose="比对页面", query_kind="menu_catalog")
    first = access.execute(session, query)
    assert first.rows[0]["candidate_paths"] == ["src/Order1.cs"]
    assert first.catalog_info["complete"]
    assert not first.catalog_info["cache_hit"]
    second = access.execute(session, query)
    assert second.catalog_info["cache_hit"] and len(fake.calls) == 1
    query.refresh_catalog = True
    assert not access.execute(session, query).catalog_info["cache_hit"]
    with pytest.raises(ValueError, match="page_lookup"):
        access.execute(session, query, "business_data")


@pytest.mark.parametrize("flow", ["development", "incident"])
def test_both_flows_return_catalog_to_model_and_persist_only_metadata(tmp_path, flow):
    session, fake, access = configured(tmp_path)
    query = DataQuery(name="menus", purpose="比对页面", query_kind="menu_catalog")
    if flow == "incident":
        runtime = ScriptedStructuredRuntime(
            [
                IncidentDecision(
                    status="query_required",
                    message="读取目录",
                    query_stage="page_lookup",
                    queries=[query],
                ),
                IncidentDecision(
                    status="needs_input", message="候选待核对", question="请确认页面标题"
                ),
            ]
        )
        engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "tasks"), None)
    else:
        runtime = ScriptedRuntime(
            AgentDecision(status="query_required", message="读取目录", queries=[query]),
            AgentDecision(status="needs_input", message="候选待核对"),
        )
        engine = _app(tmp_path / "tasks", runtime)._engine
        engine.capabilities.knowledge_root = None
    engine.database_access = access
    result = engine.start(session.workspace, "定位页面", project="ace-one")
    assert result.status.value == "needs_input"
    assert "src/Order1.cs" in runtime.turns[-1].user_message
    saved = engine.get_session(result.session_id)
    assert saved.query_observations[0].catalog_info["complete"]
    assert "Client.Order1" not in saved.model_dump_json()
    events = [e for e in saved.events if e.type.value == "database_queries_executed"]
    assert events[-1].data["queries"][0]["catalog_info"]["complete"]


def test_configuration_rejects_ambiguous_blocks(tmp_path):
    configured(tmp_path)
    p = tmp_path / "knowledge/navigation/ace-one/ace-one.md"
    p.write_text(p.read_text() + "\n" + p.read_text(), encoding="utf-8")
    with pytest.raises(ValueError, match="只能"):
        load_navigation_config("ace-one", tmp_path / "knowledge")


@pytest.mark.parametrize("flow", ["development", "incident"])
def test_stop_during_catalog_becomes_pause_not_query_failure(tmp_path, flow):
    session, fake, access = configured(tmp_path)
    query = DataQuery(name="menus", purpose="比对页面", query_kind="menu_catalog")
    if flow == "incident":
        runtime = ScriptedStructuredRuntime(
            [
                IncidentDecision(
                    status="query_required",
                    message="目录",
                    query_stage="page_lookup",
                    queries=[query],
                )
            ]
        )
        engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "tasks"), None)
    else:
        runtime = ScriptedRuntime(
            AgentDecision(status="query_required", message="目录", queries=[query])
        )
        engine = _app(tmp_path / "tasks", runtime)._engine
        engine.capabilities.knowledge_root = None
    engine.database_access = access
    engine._stop_requested = lambda task: bool(fake.calls)
    result = engine.start(session.workspace, "页面定位", project="ace-one")
    assert result.task_state.value == "paused"
    assert len(runtime.turns) == 1
    assert not list((tmp_path / "state/navigation/cache").glob("*.json"))
