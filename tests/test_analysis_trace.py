"""公开分析过程只读投影：真实来源、跨页排重及恢复兼容。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from autocoding_agent.adapters.claude_code import _stream_activities
from autocoding_agent.adapters.sqlite_incident_store import SQLiteIncidentStore
from autocoding_agent.adapters.sqlite_task_store import SQLiteTaskStore
from autocoding_agent.application import AgentApplication
from autocoding_agent.core.models import AgentEvent, AgentSession, EventType
from autocoding_agent.database_models import QueryObservation, QuerySource
from autocoding_agent.incident.application import IncidentApplication
from autocoding_agent.incident.models import IncidentSession
from autocoding_agent.interfaces.analysis_trace import AnalysisTrace


def event(event_type: EventType, sequence: int, **data) -> AgentEvent:
    return AgentEvent(type=event_type, message="公开调查说明", sequence=sequence, data=data,
                      created_at=datetime(2026, 11, 13, tzinfo=timezone.utc)
                      + timedelta(seconds=sequence))


def test_trace_pairs_across_pages_and_preserves_turns_on_resume():
    turn = event(EventType.TURN_STARTED, 1)  # 历史开始事件没有command_id。
    started = event(EventType.TOOL_STARTED, 2, tool_name="Read", tool_use_id="tool-1",
                    path="src/order.py")
    finished = event(EventType.TOOL_FINISHED, 3, tool_name="Read", tool_use_id="tool-1",
                     path="src/order.py", is_error=False)
    for item in (started, finished):
        item.correlation_id, item.command_id = "run-1", "cmd-1"
    trace = AnalysisTrace()
    assert trace.append([turn, started])
    step = trace.sections[turn.id].steps[0]
    assert "待返回" in step.summary
    assert trace.append([started, finished])
    assert "工具已返回" in step.summary
    assert len(trace.sections[turn.id].steps) == 1
    assert not trace.append([finished])
    paused = event(EventType.STATE_TRANSITIONED, 4, to="paused", reason="用户停止")
    resumed = event(EventType.TURN_STARTED, 5)
    next_tool = finished.model_copy(update={"id": "another-event", "sequence": 6,
                                           "correlation_id": "run-2"})
    trace.append([paused, resumed, next_tool])
    assert len(trace.sections[turn.id].steps) == 2
    assert len(trace.sections[resumed.id].steps) == 1  # 不跨Runtime合并相同工具ID。
    restored = AnalysisTrace()
    restored.append([turn, started, finished, paused, resumed, next_tool])
    assert restored.sections == trace.sections
    assert trace.cursor == 6


def test_trace_labels_model_judgment_and_keeps_unverified_evidence_explicit():
    trace = AnalysisTrace()
    trace.append([event(EventType.DECISION_RECORDED, 1, assessment={
        "phase": "inspecting_code", "reason": "请求先查限额规则", "missing": ["缺少现场计数"],
        "analysis": {"goal": "限制每箱十盘", "investigation": [{
            "question": "何时计数", "scope": "Bind_Click", "done_when": "确认计数分支",
        }]},
        "evidence": [{"kind": "code", "reference": "src/order.py"}],
    })])
    step = trace.sections["legacy"].steps[0]
    assert step.source == "模型判断"
    assert "待核验" in step.detail and "缺少现场计数" in step.detail
    assert "何时计数" in step.detail and "限制每箱十盘" in step.detail
    assert "已关联操作记录" not in step.detail


@pytest.mark.parametrize("with_run_id", [False, True])
def test_legacy_tool_ids_cannot_merge_across_turns(with_run_id):
    trace = AnalysisTrace()
    for i in range(2):
        turn = event(EventType.TURN_STARTED, i * 2 + 1)
        tool = event(EventType.TOOL_FINISHED, i * 2 + 2, tool_name="Read",
                     tool_use_id="reused-id", path=f"src/file{i}.py", is_error=False)
        if with_run_id:
            tool.data["run_id"] = f"run-{i}"
        trace.append([turn, tool])
    sections = list(trace.sections.values())
    assert len(sections) == 2 and all(len(section.steps) == 1 for section in sections)
    assert "file0.py" in sections[0].steps[0].summary
    assert "file1.py" in sections[1].steps[0].summary


def test_trace_uses_safe_query_audit_and_restores_older_event_details():
    trace = AnalysisTrace()
    query = event(EventType.DATABASE_QUERIES_EXECUTED, 1, queries=[{
        "name": "count-box", "sql_fingerprint": "abc", "sql": "SECRET SQL",
        "parameters": {"code": "PRIVATE VALUE"}, "rows": [{"id": "BUSINESS DATA"}],
    }])
    trace.append([query])
    observation = QueryObservation(
        query_name="count-box", purpose="确认当前数量", returned_rows=0,
        source=QuerySource(datasource_id="warehouse", name="仓库库", environment="测试",
                           database_type="oracle", schema_name="MES"),
        created_at=query.created_at,
    )
    trace.add_query_details([observation])
    step = trace.sections["legacy"].steps[0]
    before = step.detail
    trace.add_query_details([observation])
    assert step.detail == before
    assert "oracle" in step.detail and "环境标签：测试" in step.detail
    assert "返回行数：0" in step.detail and "结果截断：False" in step.detail
    assert all(text not in step.detail for text in ("SECRET SQL", "PRIVATE VALUE", "BUSINESS DATA"))


def test_trace_skips_protocol_and_private_blocks_and_marks_bounded_public_text():
    activities = _stream_activities({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "PRIVATE REASONING"},
        {"type": "text", "text": "password=secret " + "公开说明" * 500},
    ]}}, run_id="run", workspace="D:/repo", tool_context={})
    assert len(activities) == 1
    assert activities[0].summary.endswith("…（已截短）")
    assert "secret" not in activities[0].summary
    trace = AnalysisTrace()
    raw = event(EventType.RUNTIME_ACTIVITY, 1, kind="assistant_message")
    raw.message = activities[0].summary
    protocol = event(EventType.RUNTIME_ACTIVITY, 2, kind="assistant_message")
    protocol.message = '{"status":"completed"}'
    trace.append([raw, protocol, event(EventType.RUNTIME_ACTIVITY, 3, kind="heartbeat")])
    assert trace.cursor == 3
    assert len(trace.sections["legacy"].steps) == 1
    assert "PRIVATE REASONING" not in trace.sections["legacy"].steps[0].detail


@pytest.mark.parametrize("exit_code,failed", [(None, False), (0, False), (1, True)])
def test_tool_return_does_not_claim_functional_validation(exit_code, failed):
    trace = AnalysisTrace()
    trace.append([event(EventType.TOOL_FINISHED, 1, tool_name="Bash", is_error=False,
                        command="pytest", exit_code=exit_code)])
    step = trace.sections["legacy"].steps[0]
    assert step.failed == failed
    assert "测试通过" not in step.summary
    assert "命令返回状态不等于功能验收通过" in step.detail


@pytest.mark.parametrize("incident", [False, True])
def test_event_pages_are_ordered_isolated_and_do_not_load_task_snapshots(tmp_path, incident):
    store_type, session_type, app_type = (
        (SQLiteIncidentStore, IncidentSession, IncidentApplication) if incident
        else (SQLiteTaskStore, AgentSession, AgentApplication)
    )
    store = store_type(tmp_path)
    session = session_type(workspace=str(tmp_path), **({"problem": "查异常"} if incident
                                                     else {"goal": "查需求"}))
    session.events = [event(EventType.RUNTIME_ACTIVITY, i, kind="heartbeat")
                      for i in range(1, 6)]
    for item in session.events:
        item.sequence = None  # 入库时由存储分配持久序号。
    store.create(session)
    other = session_type(workspace=str(tmp_path), **({"problem": "另一个"} if incident
                                                   else {"goal": "另一个"}))
    store.create(other)
    app = app_type.__new__(app_type)
    def unexpected_load(_):
        pytest.fail("分页不应加载整个任务快照")
    app._engine = SimpleNamespace(sessions=store, get_session=unexpected_load)
    first = app.events(session.id, after_sequence=0, limit=2)
    second = app.events(session.id, after_sequence=first[-1].sequence, limit=2)
    last = app.events(session.id, after_sequence=second[-1].sequence, limit=2)
    assert [item.sequence for item in first + second + last] == [1, 2, 3, 4, 5]
    assert app.events(session.id, after_sequence=5) == []
    assert app.events(other.id, after_sequence=0) == []
    assert len(store.list_events(session.id)) == 5
    with pytest.raises(ValueError):
        app.events(session.id, after_sequence=-1)
    with pytest.raises(ValueError):
        app.events(session.id, after_sequence=0, limit=501)
    app._engine.get_session = lambda task_id: store.load(task_id)
    assert len(app.events(session.id)) == 5  # 无参旧接口仍返回全部事件。


def test_two_images_with_same_filename_remain_distinct_in_tool_trace(tmp_path):
    paths = [str(tmp_path / n / "screen.png") for n in ("a", "b")]
    ctx = {}
    for number, path in enumerate(paths, 1):
        args = dict(workspace=str(tmp_path / "repo"), tool_context=ctx, run_id="run",
                    attachment_paths=paths)
        started = _stream_activities({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": str(number), "name": "Read", "input": {"file_path": path}},
        ]}}, **args)[0]
        finished = _stream_activities({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": str(number), "content": [{"type": "image"}]},
        ]}}, **args)[0]
        assert started.data["image_number"] == number
        assert finished.data["image_delivered"]
        assert path not in finished.model_dump_json()
        trace = AnalysisTrace()
        trace.append([event(EventType.TOOL_FINISHED, 1, **finished.data)])
        assert f"图{number} · 已返回图像" in trace.sections["legacy"].steps[0].summary


def test_call_summary_survives_history_and_missing_or_interrupted_report():
    trace = AnalysisTrace()
    trace.append([
        event(EventType.RUNTIME_COMPLETED, 1, call_number=1, call_summary={
            "checked": "图2和保存入口", "finding": "重试绕过校验", "next_step": "核对提交分支"}),
        event(EventType.RUNTIME_COMPLETED, 2, call_number=2, model_message="候选尚待核对"),
        event(EventType.RUNTIME_INTERRUPTED, 3, call_number=3, terminal_reason="用户停止"),
    ])
    steps = trace.sections["legacy"].steps
    assert steps[0].summary == "重试绕过校验" and "下一步：核对提交分支" in steps[0].detail
    assert "未提供分项小结" in steps[1].detail
    assert "未完成" in steps[2].title and "用户停止" in steps[2].detail
