"""数据源别名共享查询额度，独立库可并行，排队取消或配置漂移不能执行SQL。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_datasources import catalog as catalog  # noqa: F401 - 复用已隔离的配置/凭据fixture
from test_datasources import query

from autocoding_agent.adapters.resource_lock import resource_lock
from autocoding_agent.database_models import QueryResult
from autocoding_agent.datasource_models import ProjectDataSources
from autocoding_agent.datasources import database_resource_key
from autocoding_agent.oracle_config import OracleConnectionConfig
from autocoding_agent.ports.runtime import RuntimeInterruptedError
from autocoding_agent.sqlserver_config import SQLServerConnectionConfig
from autocoding_agent.sqlserver_service import SQLServerConnectionService


def test_alias_identity_ignores_accounts_and_normalizes_target():
    first = SQLServerConnectionConfig(server="DB.EXAMPLE.", database="MES", username="first")
    second = first.model_copy(update={"server": "db.example", "database": "mes", "username": "new"})
    assert database_resource_key("sqlserver", first) == database_resource_key("sqlserver", second)
    oracle = OracleConnectionConfig(server="db.example", database="mes", username="reader")
    assert database_resource_key("oracle", oracle) != database_resource_key(
        "oracle", oracle.model_copy(update={"identifier_type": "sid"}),
    )
    assert database_resource_key("oracle", oracle) != database_resource_key("sqlserver", first)


def test_same_database_aliases_serialize_connections(catalog, monkeypatch):
    service, _, _ = catalog
    for identifier in ("ds-menu", "ds-business"):
        service.save_source(
            service.load().sources[identifier],
            SQLServerConnectionConfig(server="example", database="shared-db"),
        )
    active, maximum = 0, 0
    counter = threading.Lock()

    class Reader:
        def execute(self, request):
            nonlocal active, maximum
            with counter:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with counter:
                active -= 1
            return QueryResult(query_name=request.name, columns=[], rows=[], returned_rows=0)

    monkeypatch.setattr(SQLServerConnectionService, "_reader", lambda *args: Reader())
    snapshot = service.snapshot("project-a")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(
            lambda identifier: service.execute(snapshot, query(identifier), None),
            ["ds-menu", "ds-business"] * 4,
        ))
    assert maximum == 1
    assert len({item.source.datasource_id for item in results}) == 2
    locks = list((service.settings.data_dir / "runtime" / "datasource-locks").glob("*.lock"))
    assert len(locks) == 1
    assert locks[0].read_bytes() == b"\0"
    assert "shared-db" not in locks[0].name


def test_independent_databases_execute_in_parallel(catalog, monkeypatch):
    service, _, _ = catalog
    both_running = threading.Barrier(2)

    class Reader:
        def execute(self, request):
            both_running.wait(timeout=3)
            return QueryResult(query_name=request.name, columns=[], rows=[], returned_rows=0)

    monkeypatch.setattr(SQLServerConnectionService, "_reader", lambda *args: Reader())
    snapshot = service.snapshot("project-a")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda identifier: service.execute(snapshot, query(identifier), None),
            ["ds-menu", "ds-business"],
        ))
    assert {item.source.datasource_id for item in results} == {"ds-menu", "ds-business"}


@pytest.mark.parametrize("changed", [False, True])
def test_waiting_query_stops_or_revalidates_before_connect(catalog, monkeypatch, changed):
    service, calls, _ = catalog
    snapshot = service.snapshot("project-a")
    config = SQLServerConnectionConfig(server="example", database="ds-menu")
    lock_path = (service.settings.data_dir / "runtime" / "datasource-locks"
                 / f"{database_resource_key('sqlserver', config)}.lock")
    cancelled = threading.Event()
    validated = threading.Event()
    original = service._configuration

    def observe_configuration(*args):
        value = original(*args)
        validated.set()
        return value

    monkeypatch.setattr(service, "_configuration", observe_configuration)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with resource_lock(lock_path):
            future = executor.submit(
                service.execute, snapshot, query("ds-menu"), None,
                stop_requested=cancelled.is_set,
            )
            assert validated.wait(timeout=3)
            assert calls == []
            if changed:
                service.bind_project("project-a", ProjectDataSources(source_ids=["ds-business"]))
            else:
                cancelled.set()
                with pytest.raises(RuntimeInterruptedError, match="停止请求"):
                    future.result(timeout=3)
        if changed:
            with pytest.raises(ValueError, match="等待期间"):
                future.result(timeout=3)
    assert calls == []
