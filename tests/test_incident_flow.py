from __future__ import annotations

import json
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
from autocoding_agent.adapters.task_artifact_store import TaskArtifactStore
from autocoding_agent.adapters.workspace_snapshot import GitWorkspaceObserver
from autocoding_agent.core.artifacts.recorder import ArtifactRecorder
from autocoding_agent.core.hermes import (
    HermesSkillRequest,
    HermesSkillResult,
    HermesSkillSummary,
)
from autocoding_agent.core.models import (
    AgentUsage,
    EventType,
    MessageAttachment,
    RuntimeTurn,
)
from autocoding_agent.core.progress import ProgressPhase
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.capability_store import IncidentCapabilityStore
from autocoding_agent.incident.engine import IncidentEngine
from autocoding_agent.incident.models import (
    DataQuery,
    IncidentContinuationDecision,
    IncidentContinuationStatus,
    IncidentDecision,
    IncidentQueryStage,
    IncidentSession,
    IncidentStatus,
    LocatedPage,
    QueryObservationStatus,
    QueryResult,
)
from autocoding_agent.knowledge_rag.models import (
    KnowledgeDomain,
    KnowledgeHit,
    KnowledgeRetrievalResult,
    KnowledgeSourceType,
)
from autocoding_agent.ports.runtime import RuntimePolicyBlockedError
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class ScriptedStructuredRuntime:
    def __init__(
        self,
        decisions: Iterable[IncidentDecision | IncidentContinuationDecision],
    ) -> None:
        self.decisions = iter(decisions)
        self.turns: list[RuntimeTurn] = []

    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[IncidentDecision] | type[IncidentContinuationDecision],
    ) -> StructuredRuntimeResult[IncidentDecision] | StructuredRuntimeResult[
        IncidentContinuationDecision
    ]:
        self.turns.append(turn)
        output = next(self.decisions)
        assert isinstance(output, response_model)
        return StructuredRuntimeResult(
            output=output,
            runtime_session_id=turn.session_id,
            usage=AgentUsage(input_tokens=10, output_tokens=5, turns=1),
        )


class FakeDatabase:
    def __init__(self) -> None:
        self.queries: list[DataQuery] = []
        self.describe_calls = 0

    def describe_schema(self) -> str:
        self.describe_calls += 1
        return "orders(id INTEGER, status TEXT)"

    def execute(self, query: DataQuery) -> QueryResult:
        self.queries.append(query)
        return QueryResult(
            query_name=query.name,
            columns=["id", "status"],
            rows=[{"id": 42, "status": "stuck"}],
            returned_rows=1,
        )


class FakeHermesSkills:
    def __init__(self) -> None:
        self.requests: list[HermesSkillRequest] = []

    def available_skills(self) -> list[HermesSkillSummary]:
        return [
            HermesSkillSummary(
                name="debug-method",
                category="software-development",
                description="Trace causal evidence before proposing a fix.",
            )
        ]

    def invoke(self, request: HermesSkillRequest) -> HermesSkillResult:
        self.requests.append(request)
        return HermesSkillResult(
            skill=request.skill,
            category="software-development",
            question=request.question,
            output="Separate page identity, code origin, and data evidence before diagnosis.",
            duration_ms=8,
        )


class StubKnowledgeRetriever:
    def retrieve(
        self,
        query: str,
        *,
        domain: KnowledgeDomain,
        project: str | None = None,
        workspace_id: str | None = None,
        limit: int = 6,
    ) -> KnowledgeRetrievalResult:
        assert domain == KnowledgeDomain.INCIDENT
        assert project == "生物"
        assert workspace_id
        assert limit == 6
        return KnowledgeRetrievalResult(
            query=query,
            embedding_model="fake-hash-embedding-v1",
            simulated=True,
            hits=[
                KnowledgeHit(
                    chunk_id="chunk-incident-1",
                    document_id="document-incident-1",
                    title="Incident SQL safety",
                    heading_path="Bounded query",
                    content="Start unknown diagnostic queries with TOP 100.",
                    source_path="knowledge/incident/生物/生物.md",
                    source_type=KnowledgeSourceType.PROJECT,
                    domain=KnowledgeDomain.INCIDENT,
                    project="生物",
                    score=0.029,
                )
            ],
        )


def _page() -> LocatedPage:
    return LocatedPage(
        name="Order details",
        route="/orders/:id",
        source_paths=["src/pages/order.tsx"],
        related_paths=["src/api/orders.py"],
        explanation="The route and request handler match the report.",
    )


def test_incident_prompt_uses_dialogue_then_image_for_page_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-page-evidence"
    workspace.mkdir()
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.NEEDS_INPUT,
                message="I need the page title before locating its code.",
                question="What is the page title?",
            )
        ]
    )
    engine = IncidentEngine(runtime, JsonIncidentStore(tmp_path / "data-page-evidence"), None)

    outcome = engine.start(workspace, "The screen shows an error.")

    assert outcome.status == IncidentStatus.NEEDS_INPUT
    prompt = runtime.turns[0].system_prompt
    assert "Before inspecting any screenshot" in prompt
    assert "credible page title or page path" in prompt
    assert "image has no clear title but the conversation provides" in prompt
    assert "ask the user to confirm which" in prompt
    assert "A reliable page name is a mandatory precondition" not in prompt
    assert "at most 20 candidates" in prompt
    assert "Red\n  text is a common clue but not a rule" in prompt
    assert "Menu.NAME" not in prompt
    assert "QTMES" not in prompt


def test_completed_incident_requires_a_verified_page_source_path() -> None:
    decision = IncidentDecision(
        status=IncidentStatus.COMPLETED,
        message="Diagnosis complete.",
        page=LocatedPage(
            name="Order details",
            route="/orders/:id",
            explanation="Only a route candidate was found.",
        ),
        diagnosis="The route has not yet been verified against source code.",
    )
    with pytest.raises(ValueError, match="verified workspace-relative source path"):
        IncidentEngine._validate_decision(decision)


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
                query_stage=IncidentQueryStage.BUSINESS_DATA,
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

    progress = []
    outcome = engine.start(
        workspace,
        "Order 42 never completes",
        "/orders/42",
        project="生物",
        progress_sink=progress.append,
    )

    assert outcome.status == IncidentStatus.COMPLETED
    assert outcome.task_state == TaskState.COMPLETED
    assert outcome.page == _page()
    assert outcome.diagnosis == "Order 42 remains in the stuck state."
    assert "结论\nThe order is stuck before fulfillment." in outcome.message
    assert "为什么出现这个异常\nOrder 42 remains in the stuck state." in outcome.message
    assert "解决方法\n1. Inspect the fulfillment event consumer." in outcome.message
    assert "结论置信度\n90%" in outcome.message
    assert outcome.query_observations[0].returned_rows == 1
    assert database.queries == [query]
    assert database.describe_calls == 0
    assert len(runtime.turns) == 2
    assert runtime.turns[0].runtime_session_id is None
    assert runtime.turns[1].runtime_session_id == outcome.session_id
    assert "full catalog is intentionally omitted" in runtime.turns[0].system_prompt
    assert "orders(id INTEGER, status TEXT)" not in runtime.turns[0].system_prompt
    assert len(runtime.turns[0].system_prompt) < 16_000
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
    assert [event.phase for event in progress] == [
        ProgressPhase.PREPARING_CONTEXT,
        ProgressPhase.ANALYZING_REQUEST,
        ProgressPhase.QUERYING_DATABASE,
        ProgressPhase.DIAGNOSING_CAUSE,
        ProgressPhase.ANALYZING_REQUEST,
        ProgressPhase.SAVING_CAPABILITY,
        ProgressPhase.COMPLETED,
    ]


def test_incident_flow_injects_retrieved_knowledge_and_audits_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-rag"
    workspace.mkdir()
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Diagnosis complete.",
                page=_page(),
                diagnosis="The request is bounded before the data check.",
            )
        ]
    )
    engine = IncidentEngine(
        runtime,
        JsonIncidentStore(tmp_path / "data-rag"),
        None,
        knowledge_retriever=StubKnowledgeRetriever(),
    )

    outcome = engine.start(
        workspace,
        "The order page is stale.",
        "/orders/42",
        project="生物",
    )

    prompt = runtime.turns[0].system_prompt
    assert "<retrieved_knowledge>" in prompt
    assert "Start unknown diagnostic queries with TOP 100." in prompt
    event = next(event for event in outcome.events if event.type == EventType.KNOWLEDGE_RETRIEVED)
    assert event.data["workflow"] == "incident"
    assert event.data["simulated"] is True
    assert event.data["chunks"][0]["chunk_id"] == "chunk-incident-1"


def test_completed_incident_reopens_and_appends_to_one_capability_document(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-follow-up"
    workspace.mkdir()
    state = tmp_path / "data-follow-up"
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="First diagnosis complete.",
                page=_page(),
                diagnosis="The first symptom is explained.",
            ),
            IncidentContinuationDecision(
                status=IncidentContinuationStatus.ANSWER,
                message="Follow-up diagnosis complete.",
                diagnosis="The additional symptom has the same request boundary.",
                recommended_actions=["Reuse the verified endpoint boundary."],
                confidence=0.8,
            ),
        ]
    )
    store = JsonIncidentStore(state)
    engine = IncidentEngine(
        runtime,
        store,
        None,
        capabilities=IncidentCapabilityStore(state),
        model="test-model",
    )

    first = engine.start(workspace, "Order page is stale", "/orders/42")
    first_document = Path(first.capability_document or "")
    first_content = first_document.read_text(encoding="utf-8")
    saved = store.load(first.session_id)
    saved.query_rounds = 2
    store.save(saved)
    second = engine.send(
        first.session_id,
        "Continue: does the refresh action use the same endpoint?",
        command_id="incident-follow-up-2",
    )
    duplicate = engine.send(
        first.session_id,
        "Continue: does the refresh action use the same endpoint?",
        command_id="incident-follow-up-2",
    )
    session = store.load(first.session_id)
    second_document = Path(second.capability_document or "")

    assert second.status == duplicate.status == IncidentStatus.COMPLETED
    assert second.cycle_number == duplicate.cycle_number == 2
    assert session.cycle_number == 2
    assert session.query_rounds == 0
    assert session.cycle_objective == (
        "Continue: does the refresh action use the same endpoint?"
    )
    assert len(runtime.turns) == 2
    assert runtime.turns[1].tools == []
    assert runtime.turns[1].allowed_tools == []
    assert runtime.turns[1].runtime_session_id is None
    assert runtime.turns[1].session_id != first.session_id
    assert len(runtime.turns[1].system_prompt) < 2_000
    assert "<previous_incident_summary>" in runtime.turns[1].user_message
    assert session.located_page == _page()
    assert first_document.is_file()
    assert second_document.is_file()
    assert first_document == second_document
    assert second_document.name == f"{first.session_id}.md"
    updated_content = second_document.read_text(encoding="utf-8")
    assert first_content != updated_content
    assert "The first symptom is explained." in updated_content
    assert "cycle_count: 2" in updated_content
    assert "last_cycle_number: 2" in updated_content
    assert "## 后续诊断轮次 2" in updated_content
    assert "does the refresh action use the same endpoint" in updated_content
    capability_dir = second_document.parent
    assert len(list(capability_dir.glob("*.md"))) == 1
    task_record = json.loads(
        (capability_dir.parent / "tasks" / f"{first.session_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert task_record["cycle_count"] == 2
    assert [item["cycle_number"] for item in task_record["cycles"]] == [1, 2]
    index = (capability_dir.parent / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert index.count(f"capabilities/{first.session_id}.md") == 1
    assert "共 2 轮" in index
    event_types = [event.type for event in session.events]
    assert event_types.count(EventType.TASK_REOPENED) == 1
    assert event_types.count(EventType.TASK_COMPLETED) == 2
    assert event_types.count(EventType.CAPABILITY_SAVED) == 2


def test_completed_follow_up_escalates_without_losing_verified_page(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-follow-up-escalation"
    workspace.mkdir()
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Initial diagnosis complete.",
                page=_page(),
                diagnosis="The original stale state was explained.",
            ),
            IncidentContinuationDecision(
                status=IncidentContinuationStatus.INVESTIGATE,
                message="The user reported a new environment-specific symptom.",
            ),
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="The new symptom was checked against the existing page.",
                diagnosis="The new environment changes the request boundary.",
                recommended_actions=["Verify the environment-specific endpoint setting."],
            ),
        ]
    )
    store = JsonIncidentStore(tmp_path / "data-follow-up-escalation")
    engine = IncidentEngine(runtime, store, None)

    first = engine.start(workspace, "The page is stale", "/orders/42")
    second = engine.send(first.session_id, "Production now shows a different timeout.")

    assert second.status == IncidentStatus.COMPLETED
    assert second.cycle_number == 2
    assert len(runtime.turns) == 3
    assert runtime.turns[1].tools == []
    assert runtime.turns[1].runtime_session_id is None
    assert runtime.turns[2].runtime_session_id == first.session_id
    assert runtime.turns[2].tools == ["Read", "Glob", "Grep"]
    assert second.page == _page()
    routed = [
        event
        for event in second.events
        if event.type == EventType.RUNTIME_FINISHED
        and event.data.get("prompt_profile") == "continuation_compact"
    ]
    assert len(routed) == 1


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
                query_stage=IncidentQueryStage.PAGE_LOOKUP,
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
    assert runtime.turns[0].tools == ["Read"]
    assert runtime.turns[0].allowed_tools == ["Read"]
    assert "Source search is currently locked" in runtime.turns[0].system_prompt
    assert runtime.turns[1].tools == ["Read", "Glob", "Grep"]
    assert "page-mapping candidate" in runtime.turns[1].system_prompt
    assert "Never print SQL as an instruction to the user" in runtime.turns[0].system_prompt
    observation = outcome.query_observations[0]
    assert observation.sql_fingerprint is not None
    assert observation.parameter_names == ["page_name"]
    persisted = (tmp_path / "data" / "incidents" / f"{outcome.session_id}.json").read_text(
        encoding="utf-8"
    )
    assert "良率上传%" not in persisted
    assert "SELECT NAME, URL" not in persisted


def test_page_lookup_budget_does_not_consume_business_query_budget(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-stage-budgets"
    workspace.mkdir()
    exact_query = DataQuery(
        name="menu_exact",
        purpose="Try the supplied page title first.",
        sql="SELECT NAME, URL FROM Menu WHERE NAME = :name",
        parameters={"name": "小米良率上传"},
        max_rows=20,
    )
    fuzzy_query = DataQuery(
        name="menu_fuzzy",
        purpose="Find the closest bounded page candidate.",
        sql="SELECT NAME, URL FROM Menu WHERE NAME LIKE :keyword",
        parameters={"keyword": "%良率%"},
        max_rows=20,
    )
    business_query = DataQuery(
        name="yield_upload_rows",
        purpose="Inspect the affected upload rows from verified code semantics.",
        sql="SELECT id, status FROM orders WHERE id = :id",
        parameters={"id": 42},
    )
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Trying the exact page mapping.",
                query_stage=IncidentQueryStage.PAGE_LOOKUP,
                queries=[exact_query],
            ),
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Trying a bounded fuzzy page mapping.",
                query_stage=IncidentQueryStage.PAGE_LOOKUP,
                queries=[fuzzy_query],
            ),
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="The page is verified; checking the affected business rows.",
                page=_page(),
                query_stage=IncidentQueryStage.BUSINESS_DATA,
                queries=[business_query],
            ),
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Diagnosis complete.",
                page=_page(),
                diagnosis="The affected row remains in the pending state.",
            ),
        ]
    )

    class StageDatabase(FakeDatabase):
        def execute(self, query: DataQuery) -> QueryResult:
            self.queries.append(query)
            if query.name == "menu_exact":
                return QueryResult(
                    query_name=query.name,
                    columns=["NAME", "URL"],
                    rows=[],
                    returned_rows=0,
                )
            if query.name == "menu_fuzzy":
                return QueryResult(
                    query_name=query.name,
                    columns=["NAME", "URL"],
                    rows=[
                        {
                            "NAME": "小米良率数据上传",
                            "URL": "Ckhy.MES.Client.CKClient.CustomYieldUpLoad",
                        }
                    ],
                    returned_rows=1,
                )
            return QueryResult(
                query_name=query.name,
                columns=["id", "status"],
                rows=[{"id": 42, "status": "pending"}],
                returned_rows=1,
            )

    database = StageDatabase()
    store = JsonIncidentStore(tmp_path / "data-stage-budgets")
    engine = IncidentEngine(runtime, store, database)

    outcome = engine.start(workspace, "小米良率上传页面的数据不正确", "小米良率上传")

    assert outcome.status == IncidentStatus.COMPLETED
    assert database.queries == [exact_query, fuzzy_query, business_query]
    saved = store.load(outcome.session_id)
    assert saved.query_rounds == 3
    assert saved.page_query_rounds == 2
    assert saved.business_query_rounds == 1
    assert saved.query_repair_rounds == 0
    assert [turn.tools for turn in runtime.turns] == [
        ["Read"],
        ["Read"],
        ["Read", "Glob", "Grep"],
        ["Read", "Glob", "Grep"],
    ]
    assert [item.stage for item in outcome.query_observations] == [
        IncidentQueryStage.PAGE_LOOKUP.value,
        IncidentQueryStage.PAGE_LOOKUP.value,
        IncidentQueryStage.BUSINESS_DATA.value,
    ]


def test_incident_flow_restores_verified_page_when_model_omits_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-page-continuity"
    workspace.mkdir()
    first_query = DataQuery(
        name="upload_log_summary",
        purpose="Inspect the verified page's upload-log summary.",
        sql="SELECT id, status FROM orders WHERE id = :id",
        parameters={"id": 42},
    )
    second_query = DataQuery(
        name="upload_log_detail",
        purpose="Inspect one additional bounded detail needed for diagnosis.",
        sql="SELECT id, status FROM orders WHERE id = :id",
        parameters={"id": 43},
    )
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="The page is verified; checking upload logs.",
                page=_page(),
                query_stage=IncidentQueryStage.BUSINESS_DATA,
                queries=[first_query],
            ),
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Checking one additional detail.",
                query_stage=IncidentQueryStage.BUSINESS_DATA,
                queries=[second_query],
            ),
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Diagnosis complete.",
                diagnosis="The second row confirms the affected state.",
            ),
        ]
    )
    database = FakeDatabase()
    store = JsonIncidentStore(tmp_path / "data-page-continuity")
    engine = IncidentEngine(runtime, store, database)

    outcome = engine.start(workspace, "The verified upload page has no saved log")

    assert outcome.status == IncidentStatus.COMPLETED
    assert outcome.page == _page()
    assert database.queries == [first_query, second_query]
    saved = store.load(outcome.session_id)
    assert saved.located_page == _page()
    repairs = [event for event in saved.events if event.type == EventType.DECISION_REPAIRED]
    assert len(repairs) == 2
    assert all(event.data["repair"] == "reuse_verified_page" for event in repairs)
    assert '"name": "Order details"' in runtime.turns[1].user_message
    assert saved.query_repair_rounds == 0
    assert [turn.tools for turn in runtime.turns] == [
        ["Read"],
        ["Read", "Glob", "Grep"],
        ["Read", "Glob", "Grep"],
    ]
    assert [item.stage for item in outcome.query_observations] == [
        IncidentQueryStage.BUSINESS_DATA.value,
        IncidentQueryStage.BUSINESS_DATA.value,
    ]


def test_incident_flow_retries_one_correctable_source_search_policy_block(
    tmp_path: Path,
) -> None:
    class RecoveringRuntime:
        def __init__(self) -> None:
            self.turns: list[RuntimeTurn] = []

        def run_structured(
            self,
            turn: RuntimeTurn,
            response_model: type[IncidentDecision],
        ) -> StructuredRuntimeResult[IncidentDecision]:
            assert response_model is IncidentDecision
            self.turns.append(turn)
            if len(self.turns) == 1:
                raise RuntimePolicyBlockedError(
                    "blocked Grep",
                    policy="bounded_source_search",
                    operation="Grep",
                    reason="目录级 Grep 必须提供 glob 或 type 文件过滤器",
                    retryable=True,
                )
            return StructuredRuntimeResult(
                output=IncidentDecision(
                    status=IncidentStatus.COMPLETED,
                    message="Diagnosis complete after the corrected narrow search.",
                    page=_page(),
                    diagnosis="The verified code path explains the missing log.",
                ),
                runtime_session_id=turn.session_id,
                usage=AgentUsage(input_tokens=10, output_tokens=5, turns=1),
            )

    workspace = tmp_path / "workspace-search-repair"
    workspace.mkdir()
    runtime = RecoveringRuntime()
    store = JsonIncidentStore(tmp_path / "data-search-repair")
    engine = IncidentEngine(runtime, store, None)

    outcome = engine.start(workspace, "The upload page does not save a log")

    assert outcome.status == IncidentStatus.COMPLETED
    assert len(runtime.turns) == 2
    assert runtime.turns[1].runtime_session_id == outcome.session_id
    assert "include a language-appropriate glob or type" in runtime.turns[1].user_message
    saved = store.load(outcome.session_id)
    repair_events = [
        event for event in saved.events if event.type == EventType.POLICY_REPAIR_REQUESTED
    ]
    assert len(repair_events) == 1
    assert repair_events[0].data["operation"] == "Grep"
    assert any("正在自动要求 Agent 缩小范围" in item.content for item in saved.messages)


def test_incident_flow_stops_after_one_source_search_policy_correction(
    tmp_path: Path,
) -> None:
    class RepeatedlyBlockedRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def run_structured(
            self,
            turn: RuntimeTurn,
            response_model: type[IncidentDecision],
        ) -> StructuredRuntimeResult[IncidentDecision]:
            assert response_model is IncidentDecision
            self.calls += 1
            raise RuntimePolicyBlockedError(
                "blocked Grep",
                policy="bounded_source_search",
                operation="Grep",
                reason="目录级 Grep 必须提供 glob 或 type 文件过滤器",
                retryable=True,
            )

    workspace = tmp_path / "workspace-search-repair-limit"
    workspace.mkdir()
    runtime = RepeatedlyBlockedRuntime()
    store = JsonIncidentStore(tmp_path / "data-search-repair-limit")
    engine = IncidentEngine(runtime, store, None)

    outcome = engine.start(workspace, "The upload page does not save a log")

    assert outcome.status == IncidentStatus.FAILED
    assert runtime.calls == 2
    saved = store.load(outcome.session_id)
    assert sum(
        event.type == EventType.POLICY_REPAIR_REQUESTED for event in saved.events
    ) == 1


def test_incident_flow_allows_one_correction_in_each_successful_stage(
    tmp_path: Path,
) -> None:
    query = DataQuery(
        name="upload_log_rows",
        purpose="Inspect bounded upload-log evidence.",
        sql="SELECT id, status FROM orders WHERE id = :id",
        parameters={"id": 42},
    )

    class TwoStageRecoveringRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def run_structured(
            self,
            turn: RuntimeTurn,
            response_model: type[IncidentDecision],
        ) -> StructuredRuntimeResult[IncidentDecision]:
            assert response_model is IncidentDecision
            self.calls += 1
            if self.calls in {1, 3}:
                raise RuntimePolicyBlockedError(
                    "blocked Grep",
                    policy="bounded_source_search",
                    operation="Grep",
                    reason=(
                        "Grep 必须设置 1..100 的 head_limit"
                        if self.calls == 1
                        else "目录级 Grep 必须提供 glob 或 type 文件过滤器"
                    ),
                    retryable=True,
                )
            if self.calls == 2:
                return StructuredRuntimeResult(
                    output=IncidentDecision(
                        status=IncidentStatus.QUERY_REQUIRED,
                        message="The page is verified; checking the affected log rows.",
                        page=_page(),
                        query_stage=IncidentQueryStage.BUSINESS_DATA,
                        queries=[query],
                    ),
                    runtime_session_id=turn.session_id,
                    usage=AgentUsage(input_tokens=10, output_tokens=5, turns=1),
                )
            return StructuredRuntimeResult(
                output=IncidentDecision(
                    status=IncidentStatus.COMPLETED,
                    message="Diagnosis complete after both bounded corrections.",
                    page=_page(),
                    diagnosis="The code and bounded log row explain the symptom.",
                ),
                runtime_session_id=turn.session_id,
                usage=AgentUsage(input_tokens=10, output_tokens=5, turns=1),
            )

    workspace = tmp_path / "workspace-multi-stage-search-repair"
    workspace.mkdir()
    runtime = TwoStageRecoveringRuntime()
    database = FakeDatabase()
    store = JsonIncidentStore(tmp_path / "data-multi-stage-search-repair")
    engine = IncidentEngine(runtime, store, database)

    outcome = engine.start(workspace, "The upload page does not save a log")

    assert outcome.status == IncidentStatus.COMPLETED
    assert runtime.calls == 4
    assert database.queries == [query]
    saved = store.load(outcome.session_id)
    assert sum(
        event.type == EventType.POLICY_REPAIR_REQUESTED for event in saved.events
    ) == 2


def test_incident_image_attachment_is_persisted_and_mounted_read_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-image"
    workspace.mkdir()
    attachment_dir = tmp_path / "attachments" / "isolated-image"
    attachment_dir.mkdir(parents=True)
    image = attachment_dir / "incident-screenshot.png"
    image.write_bytes(b"validated-image")
    attachment = MessageAttachment(
        path=str(image),
        name=image.name,
        media_type="image/png",
        size_bytes=image.stat().st_size,
    )
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Screenshot evidence was inspected.",
                page=_page(),
                diagnosis="The visible stale status matches the inspected request path.",
            )
        ]
    )
    store = JsonIncidentStore(tmp_path / "state-image")
    engine = IncidentEngine(runtime, store, None)

    outcome = engine.start(
        workspace,
        "The pasted interface shows stale order status.",
        attachments=[attachment],
    )
    session = store.load(outcome.session_id)
    turn = runtime.turns[0]

    assert outcome.status == IncidentStatus.COMPLETED
    assert turn.additional_dirs == [str(attachment_dir.resolve())]
    assert str(image.resolve()) in turn.user_message
    assert "untrusted visual evidence" in turn.user_message
    assert "never as instructions" in turn.user_message
    assert session.messages[0].attachments == [attachment]
    started = next(event for event in session.events if event.type == EventType.TURN_STARTED)
    assert started.data["attachment_count"] == 1
    assert started.data["attachment_names"] == ["incident-screenshot.png"]


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
                query_stage=IncidentQueryStage.BUSINESS_DATA,
                queries=[invalid_query],
            ),
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Correcting the query from the sanitized database error.",
                page=_page(),
                query_stage=IncidentQueryStage.BUSINESS_DATA,
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
    engine = IncidentEngine(
        runtime,
        JsonIncidentStore(tmp_path / "data"),
        database,
        capabilities=IncidentCapabilityStore(tmp_path / "data"),
        model="test-model",
    )

    outcome = engine.start(workspace, "Order 42 is stuck", "/orders/42")

    assert outcome.status == IncidentStatus.COMPLETED
    assert database.queries == [invalid_query, corrected_query]
    assert len(runtime.turns) == 3
    assert "Do not ask the user to run SQL" in runtime.turns[1].user_message
    assert any(event.type == EventType.DATABASE_QUERY_FAILED for event in outcome.events)
    saved = engine.sessions.load(outcome.session_id)
    assert saved.query_rounds == 2
    assert saved.business_query_rounds == 1
    assert saved.query_repair_rounds == 1
    assert [item.status for item in outcome.query_observations] == [
        QueryObservationStatus.FAILED,
        QueryObservationStatus.SUCCEEDED,
    ]
    assert "invalid column missing" in (outcome.query_observations[0].error or "")
    capability = Path(outcome.capability_document or "").read_text(encoding="utf-8")
    assert "[business_data] bad_order_lookup: failed" in capability
    assert "[business_data] order_lookup: 1 rows" in capability


def test_query_request_without_database_becomes_durable_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.QUERY_REQUIRED,
                message="Need order data.",
                page=_page(),
                query_stage=IncidentQueryStage.BUSINESS_DATA,
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


def test_legacy_query_decision_infers_stage_even_when_task_state_exists() -> None:
    session = IncidentSession.model_validate(
        {
            "workspace": r"D:\legacy-workspace",
            "problem": "Locate the page",
            "task_state": TaskState.QUERYING_DATA.value,
            "status": IncidentStatus.QUERY_REQUIRED.value,
            "last_decision": {
                "status": IncidentStatus.QUERY_REQUIRED.value,
                "message": "Resolve the page mapping.",
                "queries": [
                    {
                        "name": "menu",
                        "purpose": "Locate page.",
                        "sql": "SELECT NAME, URL FROM Menu WHERE NAME = :name",
                        "parameters": {"name": "Orders"},
                    }
                ],
            },
        }
    )

    assert session.last_decision is not None
    assert session.last_decision.query_stage == IncidentQueryStage.PAGE_LOOKUP
    with pytest.raises(ValueError, match="query_stage is required"):
        IncidentDecision(
            status=IncidentStatus.QUERY_REQUIRED,
            message="Need data",
            queries=[
                DataQuery(
                    name="menu",
                    purpose="Locate the page.",
                    sql="SELECT NAME, URL FROM Menu WHERE NAME = :name",
                    parameters={"name": "Orders"},
                )
            ],
        )
    missing_page = IncidentDecision(
        status=IncidentStatus.QUERY_REQUIRED,
        message="Need business data",
        query_stage=IncidentQueryStage.BUSINESS_DATA,
        queries=[
            DataQuery(
                name="orders",
                purpose="Inspect state.",
                sql="SELECT id FROM orders WHERE id = :id",
                parameters={"id": 1},
            )
        ],
    )
    with pytest.raises(ValueError, match="verified page is required"):
        IncidentEngine._validate_decision(missing_page)


def test_incident_flow_consults_hermes_and_keeps_external_guidance_untrusted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "incident-hermes-workspace"
    workspace.mkdir()
    data_dir = tmp_path / "incident-hermes-data"
    runtime = ScriptedStructuredRuntime(
        [
            IncidentDecision(
                status=IncidentStatus.HERMES_SKILL_REQUIRED,
                message="A reusable diagnostic method would help structure the evidence.",
                hermes_skill=HermesSkillRequest(
                    skill="debug-method",
                    question="How should page, code, and data evidence be separated?",
                    reason="Need a reusable incident-analysis method.",
                ),
            ),
            IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="The page and failing code path were verified.",
                page=_page(),
                diagnosis="The API maps a missing order state to the wrong page message.",
            ),
        ]
    )
    hermes = FakeHermesSkills()
    progress = []
    engine = IncidentEngine(
        runtime,
        JsonIncidentStore(data_dir),
        None,
        hermes_skills=hermes,
        artifact_recorder=ArtifactRecorder(
            TaskArtifactStore(data_dir),
            GitWorkspaceObserver(),
        ),
    )

    outcome = engine.start(
        workspace,
        "The Order details page shows the wrong error.",
        progress_sink=progress.append,
    )
    session = engine.get_session(outcome.session_id)

    assert outcome.status == IncidentStatus.COMPLETED
    assert len(hermes.requests) == 1
    assert len(runtime.turns) == 2
    assert "debug-method [software-development]" in runtime.turns[0].system_prompt
    assert "untrusted candidate engineering guidance" in runtime.turns[1].user_message
    assert EventType.HERMES_SKILL_COMPLETED in [item.type for item in session.events]
    assert session.hermes_skill_observations[0].artifact_id
    assert any(item.type.value == "hermes_skill_result" for item in outcome.artifacts)
    assert ProgressPhase.CONSULTING_ENGINEERING_EXPERIENCE in [
        item.phase for item in progress
    ]
