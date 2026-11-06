"""两条真实应用流程只产出修复审阅材料，保存与恢复均不会执行写SQL。"""

import pytest
from test_agent_flows import ScriptedRuntime
from test_data_repair import repair_context, repair_plan
from test_incident_flow import ScriptedStructuredRuntime, _page

from autocoding_agent.adapters.database_safety import validate_read_only_sql
from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision
from autocoding_agent.core.progress import ProgressPhase
from autocoding_agent.core.workflow import WorkflowAssessment
from autocoding_agent.database_models import DataQuery, QueryResult, QuerySource
from autocoding_agent.datasources import TaskDatabaseAccess
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import IncidentDecision


@pytest.mark.parametrize("workflow", ["development", "incident"])
@pytest.mark.parametrize("unresolved", [False, True])
def test_bound_query_to_repair_plan_persists_without_writes_or_implicit_approval(
    tmp_path, workflow, unresolved,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _, metadata = repair_context(workspace)
    calls = []

    def read_query(snapshot, query, stage):
        validate_read_only_sql(query.sql)  # 任何意外的修复/回退执行都会使测试失败。
        query.datasource_id = query.datasource_id or snapshot.binding.business_default
        calls.append(query.model_copy(deep=True))
        return QueryResult(
            query_name=query.name,
            source=QuerySource.model_validate(snapshot.sources[query.datasource_id].model_dump()),
            columns=["ID", "STATE"], rows=[{"ID": 17, "STATE": "PRIVATE_RAW_VALUE"}],
            returned_rows=1,
        )

    metadata.service.execute = read_query
    query = DataQuery(name="order_state", purpose="核对目标订单旧值。",
                      datasource_id="ds-business",
                      sql="SELECT ID, STATE FROM MES.ORDERS WHERE ID = :id", parameters={"id": 17})
    assessment = WorkflowAssessment(
        phase=ProgressPhase.DIAGNOSING_CAUSE, reason="只提供数据修复草案。",
        user_questions=["应恢复原状态还是撤销订单？请确认业务目标。"] if unresolved else [],
    )
    if workflow == "development":
        runtime = ScriptedRuntime(
            AgentDecision(status="query_required", message="先核对数据。", queries=[query]),
            AgentDecision(status="completed", message="数据修复方案已形成。",
                          assessment=assessment, data_repair=repair_plan()),
        )
        build = build_application
    else:
        runtime = ScriptedStructuredRuntime([
            IncidentDecision(status="query_required", message="先核对数据。", page=_page(),
                             query_stage="business_data", queries=[query]),
            IncidentDecision(status="completed", message="数据修复方案已形成。", page=_page(),
                             diagnosis="订单状态与期望不符，需要人工审阅修复范围。",
                             recommended_actions=["审阅数据修复方案并核对环境。"],
                             assessment=assessment, data_repair=repair_plan()),
        ])
        build = build_incident_application
    settings = Settings(data_dir=tmp_path / "state", rag_enabled=False, claude_command="unused")
    app = build(settings=settings, runtime=runtime)
    app._engine.capabilities.knowledge_root = None
    app._engine.database_access = TaskDatabaseAccess(metadata.service, None, None)
    outcome = app.start(workspace, "核对并提出订单数据修复方案", project="project-a")
    saved = app.get_session(outcome.session_id)
    assert len(runtime.turns) == 2
    assert len(calls) == 1 and calls[0].sql == query.sql
    query_event = next(e for e in saved.events if e.type.value == "database_queries_executed")
    query_audit = query_event.data["queries"][0]
    if workflow == "incident":
        assert query_event.data["query_stage"] == "business_data"
        assert query_audit["source"]["datasource_id"] == "ds-business"
    else:
        assert query_audit["datasource_id"] == "ds-business"
    assert not getattr(saved, "pending_approval", None)
    assert not list(workspace.iterdir())
    assert "PRIVATE_RAW_VALUE" not in saved.model_dump_json()
    if unresolved:
        assert outcome.task_state.value == "waiting_input"
        assert "应恢复原状态还是撤销订单" in outcome.message
        assert repair_plan().repair_sql not in outcome.message
    else:
        assert outcome.task_state.value == "completed"
        assert saved.last_decision.data_repair.source.environment == "验收"
        assert saved.last_decision.data_repair.review_notes == []
        assert "数据修复方案（仅供审阅，未执行）" in outcome.message
        assert repair_plan().repair_sql in outcome.message
        restored = build(settings=settings, runtime=runtime).get_session(outcome.session_id)
        assert restored.last_decision.data_repair == saved.last_decision.data_repair
        assert len(calls) == 1 and len(runtime.turns) == 2
    if workflow == "development":
        with pytest.raises(ValueError, match="no pending approval"):
            app.approve(outcome.session_id)
        assert len(runtime.turns) == 2
