"""数据修复只形成审阅材料；SQL 模板与可执行的 DataQuery 严格分开。"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from autocoding_agent.adapters.database_safety import validate_read_only_sql
from autocoding_agent.core.workflow import cycle_events
from autocoding_agent.database_models import QuerySource

PlanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
PlanSQL = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]


class DataRepairPlan(BaseModel):
    """参数化修复草案，不是数据库写授权，也不交给查询执行器。"""

    datasource_id: str = Field(description="当前项目明确绑定的业务数据源ID，不沿用菜单库。")
    dialect: Literal["oracle", "sqlserver"] = Field(
        description="与绑定源一致的SQL方言；模板占位符Oracle为:name，SQL Server为@name。",
    )
    summary: PlanText = Field(description="修复目标及预期数据变化。")
    table: PlanText = Field(description="带Schema/Owner的目标表；一份方案只处理一个目标表。")
    scope: PlanText = Field(description="主键范围、旧值条件及排除项，禁止笼统描述全部数据。")
    expected_rows: int | None = Field(
        default=None, ge=1, description="预期受影响行数；未知则为空。",
    )
    parameters: dict[str, PlanText] = Field(
        default_factory=dict, max_length=20, description="占位符名称及业务含义，不保存实际业务值。",
    )
    evidence_queries: list[PlanText] = Field(
        default_factory=list, max_length=6, description="本周期在此数据源成功执行的业务查询名称。",
    )
    precheck_sql: PlanSQL = Field(description="修复前SELECT模板，核对主键、旧值、预期影响行数。")
    repair_sql: PlanSQL = Field(description="参数化INSERT/UPDATE/DELETE模板；仅展示，禁止执行。")
    verify_sql: PlanSQL = Field(description="修复后SELECT模板，核对目标值及影响边界。")
    rollback_sql: PlanSQL = Field(description="基于留存旧值/主键的回退模板，不凭空编造旧值。")
    backup: PlanText = Field(description="执行前旧值和主键的留存办法，不在对话保存原始行。")
    transaction: PlanText = Field(description="事务及行数断言策略；异常回滚，禁止默认自动提交。")
    rollback_conditions: PlanText = Field(description="回退条件、并发变化核对和提交后回退限制。")
    risks: list[PlanText] = Field(
        min_length=1, max_length=6, description="业务、锁、并发及关联数据风险。",
    )
    source: QuerySource | None = Field(default=None, description="宿主从任务快照填入的来源。")
    review_notes: list[str] = Field(
        default_factory=list, description="宿主核对缺口，不代表SQL已验证。",
    )


def bind_data_repair(session: Any, plan: DataRepairPlan, database_access: Any) -> DataRepairPlan:
    """不连接数据库、不执行SQL；核对任务来源、当前配置和查询记录。"""
    checked = plan.model_copy(deep=True)
    checked.source = None
    notes = []
    snapshot = session.database_binding
    source = snapshot.sources.get(plan.datasource_id) if snapshot else None
    if (snapshot is None or source is None
            or plan.datasource_id not in snapshot.binding.source_ids):
        notes.append("目标数据源不在任务的项目绑定中，需先明确正确的库和环境。")
    else:
        checked.source = QuerySource.model_validate(source.model_dump())
        if plan.dialect != source.database_type:
            notes.append("SQL方言与绑定数据源不一致，需重新生成方案。")
        try:
            if database_access.service is None:
                raise ValueError("数据源服务不可用")
            current = database_access.service.snapshot(snapshot.project)
            if (current is None or current.binding != snapshot.binding
                    or not snapshot.fingerprints.get(plan.datasource_id)
                    or current.fingerprints.get(plan.datasource_id)
                    != snapshot.fingerprints.get(plan.datasource_id)):
                raise ValueError("数据源配置已变化")
        except (ValueError, OSError):
            notes.append("当前数据源配置无法与任务快照核对，请重新确认环境后生成方案。")
    queries = {
        row.get("query_name", row.get("name"))
        for event in cycle_events(session) if event.type.value == "database_queries_executed"
        and event.data.get("query_stage") != "page_lookup"
        for row in event.data.get("queries", [])
        if row.get("status", "succeeded") == "succeeded"
        and row.get("catalog_info") is None
        and row.get("stage") != "page_lookup"
        and ((row.get("source") or {}).get("datasource_id") or row.get("datasource_id"))
        == plan.datasource_id
    }
    if not plan.evidence_queries or not set(plan.evidence_queries).issubset(queries):
        notes.append("尚缺当前周期、同一数据源的成功业务查询证据；不可仅凭代码推定数据需要修复。")
    if plan.expected_rows is None:
        notes.append("预期影响行数尚未明确，须先核对主键与旧值，不能直接执行。")
    for label, sql in (("前置核对", plan.precheck_sql), ("修复后核对", plan.verify_sql)):
        try:
            validate_read_only_sql(sql)
        except ValueError:
            notes.append(f"{label}SQL不符合只读查询规范，需修订。")
    # 这是模板完整性提示，不是SQL语义证明或可执行性认证。
    if not re.match(r"\s*(INSERT|UPDATE|DELETE)\b", plan.repair_sql, re.I):
        notes.append("本阶段仅提供INSERT/UPDATE/DELETE数据修复模板，不含DDL或存储过程执行。")
    for label, sql in (("修复", plan.repair_sql), ("回退", plan.rollback_sql)):
        if (re.match(r"\s*(UPDATE|DELETE)\b", sql, re.I)
                and not re.search(r"\bWHERE\b", sql, re.I)):
            notes.append(f"{label}模板缺少WHERE范围条件，禁止直接执行。")
    if not plan.parameters:
        notes.append("尚未说明参数含义，需使用主键和旧值参数限定范围。")
    checked.review_notes = notes
    return checked


def data_repair_text(plan: DataRepairPlan) -> str:
    """两条流程共用Markdown审阅输出；所有SQL均明确为尚未执行的模板。"""
    lines = ["## 数据修复方案（仅供审阅，未执行）", "", plan.summary, ""]
    if plan.source:
        source = plan.source
        lines.append(f"**任务绑定来源**：{source.name} / {source.environment} / "
                     f"{source.database_type} / {source.datasource_id}")
        lines.append("环境为配置标签，执行前仍需核对实际连接目标。")
    else:
        lines.append(f"**目标尚未核实**：{plan.datasource_id} / {plan.dialect}")
    rows = plan.expected_rows if plan.expected_rows is not None else "待核对"
    lines.extend(["", f"**目标表**：`{plan.table}`", "", f"**影响范围**：{plan.scope}", "",
                  f"**预期行数**：{rows}"])
    if plan.review_notes:
        lines.extend(["", "### 待补齐的依据", "", *[f"- {x}" for x in plan.review_notes]])
    lines.extend(["", "### 参数含义（实际值需核对）", "",
                  *[f"- `{key}`：{value}" for key, value in plan.parameters.items()]])
    for title, sql in (("1. 前置核对", plan.precheck_sql), ("2. 修复SQL", plan.repair_sql),
                       ("3. 修复后核对", plan.verify_sql), ("4. 回退SQL", plan.rollback_sql)):
        lines.extend(["", f"### {title}", "", "```sql", sql, "```"])
    for title, value in (("旧值留存", plan.backup), ("事务与行数控制", plan.transaction),
                         ("回退条件", plan.rollback_conditions)):
        lines.extend(["", f"### {title}", "", value])
    lines.extend(["", "### 影响与风险", "", *[f"- {x}" for x in plan.risks], "",
                  "本阶段不执行修复或回退SQL；代码修改审批不授予数据库写权限。"
                  "模板尚未经目标库语法、行数或业务验证。"])
    return "\n".join(lines)
