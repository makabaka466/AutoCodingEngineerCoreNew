from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.adapters.json_incident_store import JsonIncidentStore
from autocoding_agent.adapters.sqlite_database import (
    ReadOnlyQueryError,
    SQLiteDatabaseReader,
)
from autocoding_agent.core.models import AgentUsage, EventType, RuntimeTurn
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.capability_store import IncidentCapabilityStore
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import (
    DataQuery,
    IncidentDecision,
    IncidentStatus,
    LocatedPage,
    QueryResult,
)
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class ScriptedStructuredRuntime:
    def __init__(self, decisions: Iterable[IncidentDecision]) -> None:
        self.decisions = iter(decisions)
        self.turns: list[RuntimeTurn] = []

    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[IncidentDecision],
    ) -> StructuredRuntimeResult[IncidentDecision]:
        assert response_model is IncidentDecision
        self.turns.append(turn)
        return StructuredRuntimeResult(
            output=next(self.decisions),
            runtime_session_id=turn.session_id,
            usage=AgentUsage(input_tokens=10, output_tokens=5, turns=1),
        )


class FakeDatabase:
    def __init__(self) -> None:
        self.queries: list[DataQuery] = []

    def describe_schema(self) -> str:
        return "orders(id INTEGER, status TEXT)"

    def execute(self, query: DataQuery) -> QueryResult:
        self.queries.append(query)
        return QueryResult(
            query_name=query.name,
            columns=["id", "status"],
            rows=[{"id": 42, "status": "stuck"}],
            returned_rows=1,
        )


def _page() -> LocatedPage:
    return LocatedPage(
        name="Order details",
        route="/orders/:id",
        source_paths=["src/pages/order.tsx"],
        related_paths=["src/api/orders.py"],
        explanation="The route and request handler match the report.",
    )


def test_pinned_workspace_guidance_stays_separate_by_flow(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "data"
    development = CapabilityStore(state)
    incident = IncidentCapabilityStore(state)
    development_dir = development.prepare(workspace)
    incident_dir = incident.prepare(workspace)
    (development_dir / "pinned" / "project.md").write_text(
        "# Development project guide\n", encoding="utf-8"
    )
    (incident_dir / "pinned" / "project.md").write_text(
        "# Incident project guide\n", encoding="utf-8"
    )

    development.prepare(workspace)
    incident.prepare(workspace)

    development_index = (development_dir / "CAPABILITIES.md").read_text(encoding="utf-8")
    incident_index = (incident_dir / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert "[Development project guide](pinned/project.md)" in development_index
    assert "Incident project guide" not in development_index
    assert "[Incident project guide](pinned/project.md)" in incident_index
    assert "Development project guide" not in incident_index


def test_incident_flow_locates_page_queries_data_and_diagnoses(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    query = DataQuery(
        name="order_status",
        purpose="Check the affected order state.",
        sql="SELECT id, status FROM orders WHERE id = :order_id",
        parameters={"order_id": 42},
    )
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Located the order page; checking the reported order.",
                page=_page(),
                queries=[query],
            ),
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="The order is stuck before fulfillment.",
                page=_page(),
                diagnosis="Order 42 remains in the stuck state.",
                recommended_actions=["Inspect the fulfillment event consumer."],
                confidence=0.9,
                automation_candidate=True,
            ),
        ]
    )
    database = FakeDatabase()
    store = JsonIncidentStore(tmp_path / "data")
    database_reference = str(tmp_path / "incident.db")
    engine = IncidentEngine(
        runtime,
        store,
        database,
        database_reference=database_reference,
        capabilities=IncidentCapabilityStore(tmp_path / "data"),
        model="test-model",
    )

    outcome = engine.start(
        workspace,
        "Order 42 never completes",
        "/orders/42",
        project="生物",
    )

    assert outcome.status == IncidentStatus.COMPLETED
    assert outcome.task_state == TaskState.COMPLETED
    assert outcome.page == _page()
    assert outcome.diagnosis == "Order 42 remains in the stuck state."
    assert outcome.query_observations[0].returned_rows == 1
    assert database.queries == [query]
    assert len(runtime.turns) == 2
    assert runtime.turns[0].runtime_session_id is None
    assert runtime.turns[1].runtime_session_id == outcome.session_id
    assert store.load(outcome.session_id).database_reference == database_reference
    assert store.load(outcome.session_id).project == "生物"
    assert "selected the knowledge project '生物'" in runtime.turns[0].system_prompt
    session_file = tmp_path / "data" / "incidents" / f"{outcome.session_id}.json"
    assert '"status": "stuck"' not in session_file.read_text(encoding="utf-8")
    assert outcome.capability_document is not None
    document = Path(outcome.capability_document)
    assert document.parent.parent.name == "incident"
    assert "Order 42 remains in the stuck state." in document.read_text(encoding="utf-8")
    transitions = [
        event.data["to"]
        for event in outcome.events
        if event.type == EventType.STATE_TRANSITIONED
    ]
    assert transitions == [
        TaskState.INSPECTING.value,
        TaskState.QUERYING_DATA.value,
        TaskState.INSPECTING.value,
        TaskState.COMPLETED.value,
    ]
    assert any(event.type == EventType.DATABASE_QUERIES_EXECUTED for event in outcome.events)


def test_agent_resolves_page_with_host_executed_sql_before_page_is_known(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    menu_query = DataQuery(
        name="menu_page_location",
        purpose="Resolve the page title to its relative URL.",
        sql="SELECT NAME, URL FROM Menu WHERE NAME LIKE :page_name",
        parameters={"page_name": "良率上传%"},
    )
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="I will resolve the page through the configured menu mapping.",
                queries=[menu_query],
            ),
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="The upload page is located and the data path was diagnosed.",
                page=_page(),
                diagnosis="The page mapping points to the inspected request handler.",
                confidence=0.8,
            ),
        ]
    )
    database = FakeDatabase()
    store = JsonIncidentStore(tmp_path / "data")
    engine = IncidentEngine(runtime, store, database)

    outcome = engine.start(workspace, "良率上传页面数据为空", "良率上传")

    assert outcome.status == IncidentStatus.COMPLETED
    assert database.queries == [menu_query]
    assert len(runtime.turns) == 2
    assert "Never\n   print SQL as an instruction to the user" in runtime.turns[0].system_prompt
    observation = outcome.query_observations[0]
    assert observation.sql_fingerprint is not None
    assert observation.parameter_names == ["page_name"]
    persisted = (tmp_path / "data" / "incidents" / f"{outcome.session_id}.json").read_text(
        encoding="utf-8"
    )
    assert "良率上传%" not in persisted
    assert "SELECT NAME, URL" not in persisted


def test_failed_sql_is_returned_to_agent_for_automatic_correction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    invalid_query = DataQuery(
        name="bad_order_lookup",
        purpose="Inspect order state.",
        sql="SELECT missing FROM orders WHERE id = :id",
        parameters={"id": 42},
    )
    corrected_query = DataQuery(
        name="order_lookup",
        purpose="Inspect order state.",
        sql="SELECT id, status FROM orders WHERE id = :id",
        parameters={"id": 42},
    )
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Inspecting the affected order.",
                page=_page(),
                queries=[invalid_query],
            ),
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Correcting the query from the sanitized database error.",
                page=_page(),
                queries=[corrected_query],
            ),
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Diagnosis complete.",
                page=_page(),
                diagnosis="The order remains stuck.",
            ),
        ]
    )

    class CorrectingDatabase(FakeDatabase):
        def execute(self, query: DataQuery) -> QueryResult:
            self.queries.append(query)
            if len(self.queries) == 1:
                raise ReadOnlyQueryError("Read-only query failed: invalid column missing")
            return QueryResult(
                query_name=query.name,
                columns=["id", "status"],
                rows=[{"id": 42, "status": "stuck"}],
                returned_rows=1,
            )

    database = CorrectingDatabase()
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "data"), database)

    outcome = engine.start(workspace, "Order 42 is stuck", "/orders/42")

    assert outcome.status == IncidentStatus.COMPLETED
    assert database.queries == [invalid_query, corrected_query]
    assert len(runtime.turns) == 3
    assert "Do not ask the user to run SQL" in runtime.turns[1].user_message
    assert any(event.type == EventType.DATABASE_QUERY_FAILED for event in outcome.events)


def test_query_request_without_database_becomes_durable_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Need order data.",
                page=_page(),
                queries=[
                    DataQuery(
                        name="order",
                        purpose="Inspect state.",
                        sql="SELECT id FROM orders WHERE id = :id",
                        parameters={"id": 1},
                    )
                ],
            )
        ]
    )
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "data"), None)

    outcome = engine.start(workspace, "Order failed", "Order details")

    assert outcome.status == IncidentStatus.FAILED
    assert "no incident database is configured" in outcome.message.lower()
    assert engine.outcome(outcome.session_id).status == IncidentStatus.FAILED


def test_sqlite_reader_is_bounded_read_only_and_redacts_sensitive_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "incident.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE users(id INTEGER, email TEXT, auth_token TEXT)")
    connection.executemany(
        "INSERT INTO users VALUES (?, ?, ?)",
        [
            (1, "one@example.com", "secret-one"),
            (2, "two@example.com", "secret-two"),
            (3, "three@example.com", "secret-three"),
        ],
    )
    connection.commit()
    connection.close()
    reader = SQLiteDatabaseReader(database_path, max_rows=2)

    result = reader.execute(
        DataQuery(
            name="users",
            purpose="Inspect affected users.",
            sql="SELECT id, email, auth_token FROM users ORDER BY id",
            max_rows=10,
        )
    )

    assert "users(id INTEGER, email TEXT, auth_token TEXT)" in reader.describe_schema()
    assert result.returned_rows == 2
    assert result.truncated is True
    assert result.redacted_columns == ["auth_token"]
    assert result.rows[0]["email"] == "one@example.com"
    assert result.rows[0]["auth_token"] == "[REDACTED]"
    with pytest.raises(ReadOnlyQueryError, match="Only SELECT"):
        reader.execute(
            DataQuery(
                name="unsafe",
                purpose="Must be rejected.",
                sql="UPDATE users SET email = 'changed@example.com'",
            )
        )

    verify = sqlite3.connect(database_path)
    assert verify.execute("SELECT email FROM users WHERE id = 1").fetchone() == (
        "one@example.com",
    )
    verify.close()


def test_incident_contract_requires_question_query_and_completed_diagnosis() -> None:
    with pytest.raises(ValueError, match="question is required"):
        IncidentDecision(status=IncidentStatus.NEEDS_INPUT, message="Need context")
    with pytest.raises(ValueError, match="queries are required"):
        IncidentDecision(
            status=IncidentStatus.QUERY_REQUIRED,
            message="Need data",
            page=_page(),
        )
    with pytest.raises(ValueError, match="diagnosis is required"):
        IncidentDecision(
            status=IncidentStatus.COMPLETED,
            message="Done",
            page=_page(),
        )
