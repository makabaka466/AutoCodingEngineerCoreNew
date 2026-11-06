"""数据修复方案只核对来源与证据，绝不能把模板当成数据库写授权。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from autocoding_agent.adapters.database_safety import ReadOnlyQueryError, validate_read_only_sql
from autocoding_agent.core.data_repair import DataRepairPlan, bind_data_repair, data_repair_text
from autocoding_agent.core.models import AgentEvent, AgentSession, EventType
from autocoding_agent.database_models import QuerySource
from autocoding_agent.datasource_models import (
    DataSourceDefinition,
    ProjectDataSources,
    TaskDataSources,
)


def repair_plan(dialect="oracle"):
    return DataRepairPlan(
        datasource_id="ds-business", dialect=dialect, summary="纠正单笔订单状态。",
        table="MES.ORDERS", scope="按主键和旧状态限定，仅处理当前订单。", expected_rows=1,
        parameters={"id": "订单主键", "old_state": "核对后的旧状态", "new_state": "目标状态"},
        evidence_queries=["order_state"],
        precheck_sql="SELECT ID, STATE FROM MES.ORDERS WHERE ID = :id AND STATE = :old_state",
        repair_sql="UPDATE MES.ORDERS SET STATE = :new_state WHERE ID = :id AND STATE = :old_state",
        verify_sql="SELECT ID, STATE FROM MES.ORDERS WHERE ID = :id",
        rollback_sql=("UPDATE MES.ORDERS SET STATE = :old_state "
                      "WHERE ID = :id AND STATE = :new_state"),
        backup="执行人保存受影响主键和原值，保存到受控位置。",
        transaction="显式事务，影响行数不是1时回滚；核对后再人工提交。",
        rollback_conditions="提交后先核对状态与并发变化，只还原本次变更，不覆盖后续更新。",
        risks=["并发更新可能使旧值失效；核对失败时不得执行。"],
    )


def repair_context(tmp_path, dialect="oracle", *, development_event=False):
    source = DataSourceDefinition(
        datasource_id="ds-business", name="业务库", database_type=dialect,
        environment="验收", schema_name="MES",
    )
    snapshot = TaskDataSources(
        project="project-a",
        binding=ProjectDataSources(source_ids=[source.datasource_id],
                                   business_default=source.datasource_id),
        sources={source.datasource_id: source},
        fingerprints={source.datasource_id: "fingerprint-a"},
    )
    session = AgentSession(workspace=str(tmp_path), goal="分析数据修复", project="project-a",
                           database_binding=snapshot)
    row = {"name" if development_event else "query_name": "order_state"}
    if development_event:
        row["datasource_id"] = source.datasource_id
    else:
        row["source"] = source.model_dump()
    session.events.append(AgentEvent(
        type=EventType.DATABASE_QUERIES_EXECUTED, message="成功取得只读证据。",
        data={"query_stage": "business_data", "queries": [row]},
    ))

    class MetadataService:
        def __init__(self):
            self.current = snapshot.model_copy(deep=True)
            self.calls = 0

        def snapshot(self, project):
            assert project == snapshot.project
            self.calls += 1
            return self.current

        def execute(self, *args, **kwargs):
            raise AssertionError("方案核对不得调用数据库执行器")

    return session, SimpleNamespace(service=MetadataService())


@pytest.mark.parametrize("dialect", ["oracle", "sqlserver"])
@pytest.mark.parametrize("development_event", [False, True])
def test_plan_uses_bound_metadata_and_current_business_evidence(
    tmp_path, dialect, development_event,
):
    session, access = repair_context(tmp_path, dialect, development_event=development_event)
    plan = repair_plan(dialect)
    plan.source = QuerySource(datasource_id="ds-fabricated", name="伪造库",
                              environment="production")
    plan.review_notes = ["模型伪造的核验结论"]
    checked = bind_data_repair(session, plan, access)
    assert checked.review_notes == []
    assert checked.source.datasource_id == "ds-business"
    assert checked.source.environment == "验收"
    assert checked.source.database_type == dialect
    assert access.service.calls == 1
    assert plan.source.datasource_id == "ds-fabricated"  # 不修改原始模型返回。
    text = data_repair_text(checked)
    assert "仅供审阅，未执行" in text
    assert "代码修改审批不授予数据库写权限" in text
    assert plan.repair_sql in text and plan.rollback_sql in text
    assert "### 1. 前置核对" in text and "### 4. 回退SQL" in text


@pytest.mark.parametrize("change", ["cross_source", "legacy", "dialect"])
def test_wrong_source_or_dialect_is_never_presented_without_review_gap(tmp_path, change):
    session, access = repair_context(tmp_path)
    plan = repair_plan()
    if change == "cross_source":
        plan.datasource_id = "ds-other-project"
    elif change == "legacy":
        session.database_binding = None
    else:
        plan.dialect = "sqlserver"
    checked = bind_data_repair(session, plan, access)
    assert checked.review_notes
    if change != "dialect":
        assert checked.source is None
        assert access.service.calls == 0
    else:
        assert any("方言" in note for note in checked.review_notes)


@pytest.mark.parametrize("change", [
    "fingerprint", "authorization", "removed", "missing_fingerprint",
])
def test_source_drift_or_missing_catalog_remains_an_unverified_draft(tmp_path, change):
    session, access = repair_context(tmp_path)
    if change == "fingerprint":
        access.service.current.fingerprints["ds-business"] = "changed"
    elif change == "authorization":
        access.service.current.binding = ProjectDataSources()
    elif change == "removed":
        access.service.current = None
    else:
        session.database_binding.fingerprints.clear()
        access.service.current.fingerprints.clear()
    checked = bind_data_repair(session, repair_plan(), access)
    assert any("配置" in note for note in checked.review_notes)


@pytest.mark.parametrize("missing", [
    "none", "other_source", "catalog", "page_lookup", "failed", "old_cycle",
])
def test_missing_wrong_menu_failed_or_old_evidence_cannot_support_data_repair(tmp_path, missing):
    session, access = repair_context(tmp_path)
    event = session.events[0]
    row = event.data["queries"][0]
    if missing == "none":
        session.events.clear()
    elif missing == "other_source":
        row["source"]["datasource_id"] = "ds-menu"
    elif missing == "catalog":
        row["catalog_info"] = {"complete": True}
    elif missing == "page_lookup":
        event.data["query_stage"] = "page_lookup"
    elif missing == "failed":
        row["status"] = "failed"
    else:
        session.events.append(AgentEvent(type=EventType.TASK_REOPENED, message="新任务周期。"))
    checked = bind_data_repair(session, repair_plan(), access)
    assert any("成功业务查询证据" in note for note in checked.review_notes)


def test_incomplete_or_mutating_check_templates_are_flagged_without_running_them(tmp_path):
    session, access = repair_context(tmp_path)
    plan = repair_plan()
    plan.expected_rows = None
    plan.parameters = {}
    plan.precheck_sql = "DELETE FROM MES.ORDERS WHERE ID = :id"
    plan.verify_sql = "UPDATE MES.ORDERS SET STATE = :new_state WHERE ID = :id"
    plan.repair_sql = "DROP TABLE MES.ORDERS"
    checked = bind_data_repair(session, plan, access)
    assert any("预期影响行数" in note for note in checked.review_notes)
    assert any("参数含义" in note for note in checked.review_notes)
    assert any("前置核对SQL" in note for note in checked.review_notes)
    assert any("修复后核对SQL" in note for note in checked.review_notes)
    assert any("DDL" in note for note in checked.review_notes)


@pytest.mark.parametrize("sql", [
    "UPDATE MES.ORDERS SET STATE = :new_state WHERE ID = :id",
    "DELETE FROM dbo.orders WHERE id = @id",
    "INSERT INTO dbo.orders(id) VALUES (@id)",
])
def test_repair_sql_still_fails_the_existing_query_execution_guard(sql):
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only_sql(sql)


def test_persisted_plan_roundtrip_is_bounded_and_contains_no_parameter_values():
    plan = repair_plan()
    restored = DataRepairPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert restored.parameters["id"] == "订单主键"
    with pytest.raises(ValidationError):
        DataRepairPlan.model_validate({**plan.model_dump(), "repair_sql": "x" * 6001})
