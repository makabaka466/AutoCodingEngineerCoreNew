"""把已有任务事件投影为公开分析过程，不调用模型或改变任务状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from autocoding_agent.adapters.claude_code import _redact
from autocoding_agent.core.models import AgentEvent
from autocoding_agent.core.workflow import WorkflowAssessment, assessment_text
from autocoding_agent.database_models import QueryObservation


def public_text(value: object, limit: int = 1600) -> str:
    """兼容历史记录，展示前沿用运行时脱敏并明确标注截短。"""
    text = _redact("" if value is None else str(value)).strip()
    return text[:limit] + ("…（已截短）" if len(text) > limit else "")


def _query_detail(row: dict) -> str:
    """新事件和历史审计共用安全字段白名单，避免复制SQL、参数和结果行。"""
    lines = []
    for name, label in (("query_name", "查询"), ("name", "查询"), ("purpose", "目的"),
                        ("datasource_id", "数据源ID"), ("returned_rows", "返回行数"),
                        ("truncated", "结果截断"), ("sql_fingerprint", "SQL指纹")):
        if row.get(name) is not None:
            lines.append(f"{label}：{public_text(row[name], 500)}")
    source = row.get("source")
    if isinstance(source, dict):
        for name, label in (("name", "数据源"), ("database_type", "数据库类型"),
                            ("environment", "环境标签"), ("schema_name", "Schema")):
            if source.get(name):
                lines.append(f"{label}：{public_text(source[name], 150)}")
    if row.get("status"):
        lines.append("查询状态：" + {"succeeded": "成功", "failed": "失败"}.get(
            str(row["status"]), "未记录"))
    return "\n".join(lines)


@dataclass
class AnalysisStep:
    key: str  # 事件ID或同一工具调用ID，用于展开状态及开始/结束合并。
    time: datetime
    source: str  # 模型公开说明、模型判断、工具记录或系统记录。
    title: str
    summary: str
    detail: str = ""
    group: str = ""  # 连续同类读取/搜索只显示一个可展开的组。
    failed: bool = False  # 仅据宿主/工具失败记录提示，不从模型措辞推断。


@dataclass
class AnalysisSection:
    key: str  # 当前任务内的命令/轮次身份，不以文本内容作为身份。
    time: datetime
    steps: list[AnalysisStep] = field(default_factory=list)


def group_conversation_turns(
    entries: list[tuple[str, str]], times: list[datetime | None], sections,
) -> tuple[dict[int, AnalysisSection], set[int]]:
    """每次用户发言保留最后一条实质答复，期间的进度和较早答复仍在此轮详情可读。"""
    users = [(i, times[i]) for i, (role, _) in enumerate(entries)
             if role == "user" and i < len(times) and times[i] is not None]
    groups = {i: AnalysisSection(f"turn:{ordinal}", stamp)
              for ordinal, (i, stamp) in enumerate(users, 1)}
    for section in sections:
        if section.steps:
            index = next((i for i, stamp in reversed(users) if stamp <= section.time),
                         users[0][0] if users else -1)
            if index not in groups:
                groups[index] = AnalysisSection("legacy:" + section.key, section.time)
            groups[index].steps.extend(section.steps)

    interim = set()
    # 历史快照把宿主正在执行的查询通知也存为 assistant。
    query_updates = {"Agent 已形成最小只读查询计划，正在自动核对数据库证据。",
                     "Agent formed a bounded read-only query plan. The host is executing it "
                     "automatically; the user does not need to run SQL."}
    for ordinal, (index, start_time) in enumerate(users):
        end = users[ordinal + 1][0] if ordinal + 1 < len(users) else len(entries)
        replies = [i for i in range(index + 1, end) if entries[i][0] == "assistant"]
        final = next((i for i in reversed(replies) if entries[i][1] not in query_updates), None)
        for i in replies:
            if i == final:
                continue
            interim.add(i)
            content = entries[i][1]
            step_time = times[i] if i < len(times) and times[i] else start_time
            groups[index].steps.append(AnalysisStep(
                key=f"reply:{groups[index].key}:{i}", time=step_time, source="对话记录",
                title="查询进度" if content in query_updates else "本轮较早回复",
                summary=public_text(content, 180), detail=public_text(content, len(content)),
            ))
        if len(replies) > 1 or final is None and replies:
            groups[index].steps.sort(key=lambda step: step.time)
    return groups, interim


class AnalysisTrace:
    """每个窗口持有当前任务的投影，序号排重；历史事实继续由事件库保存。"""

    def __init__(self) -> None:
        self.cursor = 0
        self.sections: dict[str, AnalysisSection] = {}
        self._seen: set[str] = set()
        self._tools: dict[tuple[str, str], AnalysisStep] = {}
        self._active = "legacy"

    def append(self, events: list[AgentEvent]) -> bool:
        changed = False
        for event in events:
            self.cursor = max(self.cursor, event.sequence or 0)
            if event.id in self._seen:
                continue
            self._seen.add(event.id)
            kind, data = event.type.value, event.data
            if kind == "turn_started":
                self._active = event.command_id or event.id
            # 旧TURN_STARTED没有command_id；停止/恢复也可能另用命令ID。
            # 以真实轮次边界分组，避免同一轮的说明、工具和决定被拆散。
            key = self._active
            if key not in self.sections:
                self.sections[key] = AnalysisSection(key, event.created_at)
            section = self.sections[key]
            step = self._project(event)
            if step is None:
                continue
            if kind in {"tool_started", "tool_finished"} and data.get("tool_use_id"):
                run_key = event.correlation_id or str(data.get("run_id") or key)
                tool_key = (run_key, str(data["tool_use_id"]))
                previous = self._tools.get(tool_key)
                if previous is not None:
                    if kind == "tool_finished":
                        previous.summary, previous.detail = step.summary, step.detail
                        previous.failed = step.failed
                        changed = True
                    continue
                self._tools[tool_key] = step
            section.steps.append(step)
            changed = True
        return changed

    def add_query_details(self, observations: list[QueryObservation]) -> None:
        """旧开发事件只保存查询指纹时，用同任务已有查询审计补齐来源及结果摘要。"""
        for observation in observations:
            matches = [step for section in self.sections.values() for step in section.steps
                       if step.title == "数据库查询"
                       and f"查询：{observation.query_name}\n" in step.detail + "\n"]
            if not matches:
                continue
            step = min(matches, key=lambda item: abs(
                (item.time - observation.created_at).total_seconds(),
            ))
            detail = _query_detail(observation.model_dump(mode="json"))
            if detail not in step.detail:
                # 历史事件缺少结果元数据时才补齐，避免新事件重复显示相同字段。
                if f"查询：{observation.query_name}\n目的：" not in step.detail:
                    step.detail += "\n" + detail

    @staticmethod
    def _project(event: AgentEvent) -> AnalysisStep | None:
        kind, data = event.type.value, event.data
        source, title, summary, detail, group = "系统记录", "", "", "", ""
        failed = False
        if kind == "runtime_activity" and data.get("kind") == "assistant_message":
            text = event.message.strip()
            # 结构化最终决定已由decision_recorded展示，避免把协议JSON当分析说明。
            if not text or text.startswith(("{", "```json")):
                return None
            source, title, summary = "模型公开说明", "调查说明", public_text(text, 180)
            detail = public_text(text)
        elif kind in {"tool_started", "tool_finished"}:
            tool = str(data.get("tool_name") or "工具")
            source = "工具记录"
            title = {"read": "读取文件", "grep": "搜索代码", "glob": "查找文件",
                     "edit": "修改文件", "write": "写入文件", "bash": "执行命令"}.get(
                         tool.casefold(), tool)
            group = tool.casefold() if tool.casefold() in {"read", "grep", "glob"} else ""
            if kind == "tool_started":
                status = "已开始，结果待返回"
            elif data.get("is_error"):
                status = "执行失败"
                failed = True
            elif data.get("is_error") is False:
                status = "工具已返回"
            else:
                status = "已返回，状态未记录"
            if type(data.get("exit_code")) is int:
                status += f"，退出码 {data['exit_code']}"
                failed |= data["exit_code"] != 0
            target = public_text(data.get("path") or data.get("pattern") or tool, 160)
            if data.get("image_number"):
                target = f"图{data['image_number']}"
                if kind == "tool_finished" and not data.get("is_error"):
                    status = "已返回图像" if data.get("image_delivered") else "未记录图像返回"
            elif target.casefold() == "<outside_workspace>":
                target = "工作区外路径（已脱敏）"
            summary = f"{target} · {status}"
            detail = "\n".join(f"{label}：{public_text(data[name], 800)}" for name, label in (
                ("path", "路径"), ("pattern", "搜索内容"), ("command", "命令"),
            ) if data.get(name))
            detail += f"\n结果：{status}\n记录：{event.id[:8]}"
            if tool.casefold() == "bash":
                detail += "\n命令返回状态不等于功能验收通过。"
        elif kind == "runtime_completed" and data.get("call_number"):
            source, title = "模型公开说明", f"第 {data['call_number']} 次调用 · 调查小结"
            report = data.get("call_summary")
            report = report if isinstance(report, dict) else {}
            summary = public_text(report.get("finding") or data.get("model_message")
                                  or "本次调用未返回调查小结。", 240)
            detail = "\n".join(f"{label}：{public_text(report[key], 240)}"
                               for key, label in (("checked", "检查"), ("finding", "发现"),
                                                  ("next_step", "下一步")) if report.get(key))
            if not detail:
                detail = "模型未提供分项小结。\n" + summary
        elif kind == "decision_recorded" and data.get("assessment"):
            try:
                assessment = WorkflowAssessment.model_validate(data["assessment"])
            except (ValidationError, TypeError):
                return None
            source, title = "模型判断", "阶段判断与依据"
            summary = public_text(assessment.reason, 180)
            detail = public_text(assessment_text(assessment), 6000)
            references = [f"{item.reference} → 记录 {item.event_id[:8]}"
                          for item in assessment.evidence if item.verified and item.event_id]
            if references:
                detail += "\n已关联操作\n" + public_text("\n".join(references))
        elif kind in {"database_queries_executed", "database_query_failed"}:
            source, title = "工具记录", "数据库查询"
            rows = data.get("queries") or []
            rows = [row for row in rows if isinstance(row, dict)]
            failed = kind == "database_query_failed"
            summary = (f"已执行 {len(rows)} 项只读查询" if kind == "database_queries_executed"
                       else "查询失败，需核对查询条件或连接")
            detail = "\n\n".join(_query_detail(row) for row in rows)
            if failed:
                detail += "\n" + public_text(data.get("error") or event.message, 800)
            detail += "\n仅展示已有查询审计摘要，不包含原始SQL、参数值或业务结果行。"
        elif kind in {"policy_repair_requested", "decision_repaired", "recovery_required",
                      "runtime_failed", "runtime_interrupted", "verification_failed"}:
            title = "调查调整" if "repair" in kind else "执行受阻"
            if data.get("call_number") and kind in {"runtime_failed", "runtime_interrupted"}:
                title = f"第 {data['call_number']} 次调用 · 未完成"
            failed = "failed" in kind or kind == "recovery_required"
            summary = public_text(event.message, 180)
            detail = public_text(event.message)
            for name in ("missing", "reason", "terminal_reason", "operation"):
                if data.get(name):
                    detail += "\n" + public_text(data[name])
        elif kind == "state_transitioned" and data.get("to") in {
            "paused", "recovery_required", "waiting_input", "cancelled",
        }:
            title = "任务状态"
            summary = public_text(data.get("reason") or event.message, 180)
            detail = public_text(data.get("reason") or event.message)
        elif kind == "runtime_activity" and data.get("kind") == "policy_blocked":
            title, summary = "操作受阻", public_text(event.message, 180)
            detail = public_text(data.get("reason") or event.message)
            failed = True
        else:
            return None
        return AnalysisStep(
            event.id, event.created_at, source, title, summary, detail, group, failed,
        )
