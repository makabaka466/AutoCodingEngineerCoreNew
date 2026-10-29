"""完整目录、缓存隔离与读取上限测试，不连接真实数据库。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from autocoding_agent.adapters.database_safety import validate_read_only_sql
from autocoding_agent.database_models import QueryResult, QuerySource
from autocoding_agent.datasource_models import (
    DataSourceDefinition,
    ProjectDataSources,
    TaskDataSources,
)
from autocoding_agent.navigation_catalog import MenuCatalogConfig, MenuCatalogService
from autocoding_agent.ports.runtime import RuntimeInterruptedError


def menu(index, *, upper=False, path=None):
    row = {
        "menu_id": str(index),
        "menu_name": f"订单 {index}",
        "menu_path": path if path is not None else f"Client.Order{index}",
    }
    return {key.upper() if upper else key: value for key, value in row.items()}


def setup(tmp_path, rows, *, database_type="sqlserver", project="ace-one"):
    source = DataSourceDefinition(
        datasource_id="ds-menu", name="菜单", environment="test", database_type=database_type
    )
    binding = TaskDataSources(
        project=project,
        binding=ProjectDataSources(source_ids=["ds-menu"], menu_default="ds-menu"),
        sources={"ds-menu": source},
        fingerprints={"ds-menu": "same-connection"},
    )
    session = SimpleNamespace(
        project=project, workspace=str(tmp_path / "repo"), database_binding=binding
    )

    class Access:
        def __init__(self):
            self.current = binding.model_copy(deep=True)
            self.service = SimpleNamespace(snapshot=lambda project: self.current)
            self.calls = []
            self.rows = rows
            self.failure = None
            self.truncated = False

        def execute(self, active_session, query, stage):
            assert active_session is session
            assert stage == "page_lookup"
            assert query.datasource_id == "ds-menu"
            assert query.max_rows <= 100
            assert "ROW_NUMBER() OVER (ORDER BY ace_menu.menu_id)" in query.sql
            assert "FROM (" in query.sql
            validate_read_only_sql(query.sql)
            self.calls.append(query)
            if self.failure:
                raise self.failure
            start = query.parameters["ace_catalog_start"]
            end = query.parameters["ace_catalog_end"]
            page = deepcopy(self.rows[start:end])
            return QueryResult(
                query_name=query.name,
                rows=page,
                returned_rows=len(page),
                columns=list(page[0]) if page else [],
                truncated=self.truncated,
                source=QuerySource.model_validate(source.model_dump()),
            )

    config = MenuCatalogConfig(
        datasource_id="ds-menu",
        sql=(
            "SELECT id AS menu_id, title AS menu_name, url AS menu_path FROM menus "
            "WHERE language = :lang"
        ),
        parameters={"lang": 1},
        page_size=2,
    )
    return MenuCatalogService(tmp_path / "state"), session, config, Access()


@pytest.mark.parametrize("dialect", ["sqlserver", "oracle"])
def test_reads_all_pages_then_reuses_complete_cache(tmp_path, dialect):
    service, session, config, access = setup(
        tmp_path, [menu(i, upper=dialect == "oracle") for i in range(5)], database_type=dialect
    )
    result = service.load(session, config, access)
    assert result.complete and not result.cache_hit
    assert [row["menu_id"] for row in result.rows] == [str(i) for i in range(5)]
    assert len(access.calls) == 3
    assert all(query.parameters["lang"] == 1 for query in access.calls)
    again = service.load(session, config, access)
    assert again.cache_hit and again.digest == result.digest
    assert len(access.calls) == 3


@pytest.mark.parametrize("count, complete", [(0, True), (3, True), (4, False)])
def test_exact_total_limit_is_distinguished_from_truncation(tmp_path, count, complete):
    service, session, config, access = setup(tmp_path, [menu(i) for i in range(count)])
    config.max_rows = 3
    result = service.load(session, config, access)
    assert result.complete is complete
    assert len(result.rows) == min(count, 3)
    assert bool(list(service.cache_dir.glob("*.json"))) is complete


@pytest.mark.parametrize(
    "kind", ["duplicate", "missing_alias", "long_field", "adapter_truncated", "partial_page"]
)
def test_bad_or_partial_rows_never_become_a_complete_cache(tmp_path, kind):
    rows = [menu(1), menu(2), menu(3)]
    if kind == "duplicate":
        rows[2] = menu(1)
    elif kind == "missing_alias":
        rows[2].pop("menu_id")
    elif kind == "long_field":
        rows[2]["menu_path"] = "x" * 501
    elif kind == "adapter_truncated":
        rows[2]["menu_path"] = "x" * 499 + "…"
    service, session, config, access = setup(tmp_path, rows)
    access.truncated = kind == "partial_page"
    result = service.load(session, config, access)
    assert not result.complete and not result.cache_hit
    assert not list(service.cache_dir.glob("*.json"))
    service.load(session, config, access)
    assert len(access.calls) >= 2


def test_empty_paths_are_retained_as_hierarchy_only(tmp_path):
    service, session, config, access = setup(tmp_path, [menu(1, path="")])
    result = service.load(session, config, access)
    assert result.complete and result.rows[0]["menu_path"] == ""
    assert "层级" in result.reason


@pytest.mark.parametrize("change", ["fingerprint", "environment", "binding"])
def test_current_source_is_rechecked_even_on_cache_hit(tmp_path, change):
    service, session, config, access = setup(tmp_path, [menu(1)])
    service.load(session, config, access)
    if change == "fingerprint":
        access.current.fingerprints["ds-menu"] = "another-host"
    elif change == "environment":
        access.current.sources["ds-menu"].environment = "production"
    else:
        access.current.binding.menu_default = None
    with pytest.raises(ValueError, match="变化"):
        service.load(session, config, access)
    assert len(access.calls) == 1


def test_cache_key_isolates_project_workspace_and_configuration(tmp_path):
    service, session, config, access = setup(tmp_path, [menu(1)])
    service.load(session, config, access)
    session.project = "ace-two"
    session.database_binding.project = "ace-two"
    access.current.project = "ace-two"
    assert not service.load(session, config, access).cache_hit
    session.workspace = str(tmp_path / "another-repo")
    assert not service.load(session, config, access).cache_hit
    config.parameters["lang"] = 2
    assert not service.load(session, config, access).cache_hit
    assert len(access.calls) == 4


def test_refresh_expiry_and_corrupt_cache_trigger_a_new_read(tmp_path):
    service, session, config, access = setup(tmp_path, [menu(1)])
    service.load(session, config, access)
    assert not service.load(session, config, access, refresh=True).cache_hit
    cache = next(service.cache_dir.glob("*.json"))
    import json

    content = json.loads(cache.read_text(encoding="utf-8"))
    content["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    cache.write_text(json.dumps(content), encoding="utf-8")
    assert not service.load(session, config, access).cache_hit
    cache.write_text("not json", encoding="utf-8")
    assert not service.load(session, config, access).cache_hit
    assert len(access.calls) == 4


def test_failure_does_not_leak_driver_text_or_leave_refreshed_cache(tmp_path):
    service, session, config, access = setup(tmp_path, [menu(1)])
    service.load(session, config, access)
    access.failure = RuntimeError("secret=private-server-password")
    with pytest.raises(ValueError, match="目录查询失败") as caught:
        service.load(session, config, access, refresh=True)
    assert "private" not in str(caught.value)
    assert not list(service.cache_dir.glob("*.json"))


def test_stop_between_pages_does_not_execute_or_cache_remaining_pages(tmp_path):
    service, session, config, access = setup(tmp_path, [menu(i) for i in range(5)])
    with pytest.raises(RuntimeInterruptedError):
        service.load(session, config, access, stop_requested=lambda: bool(access.calls))
    assert len(access.calls) == 1
    assert not list(service.cache_dir.glob("*.json"))


def test_combined_catalog_text_is_bounded(tmp_path):
    rows = [menu(i, path="x" * 500) for i in range(500)]
    service, session, config, access = setup(tmp_path, rows)
    config.page_size = 100
    result = service.load(session, config, access)
    assert not result.complete and "文本" in result.reason
    assert len(result.rows) < 500
    assert not list(service.cache_dir.glob("*.json"))


def test_legacy_or_cross_project_connection_is_not_used(tmp_path):
    service, session, config, access = setup(tmp_path, [menu(1)])
    session.database_binding = None
    with pytest.raises(ValueError, match="项目绑定"):
        service.load(session, config, access)
    assert not access.calls


@pytest.mark.parametrize(
    "change",
    [
        dict(sql="DELETE FROM menus"),
        dict(sql="SELECT 1;"),
        dict(parameters={"ace_catalog_start": 5}),
        dict(page_size=101),
        dict(max_rows=2001),
    ],
)
def test_invalid_configuration_never_reaches_database(change):
    values = dict(datasource_id="ds-menu", sql="SELECT id AS menu_id FROM menus")
    values.update(change)
    with pytest.raises(ValidationError):
        MenuCatalogConfig(**values)
