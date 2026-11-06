"""以真实线程、进程及应用快照验证调度，不依赖真实模型或业务数据库。"""

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from uuid import uuid4

import pytest
from test_approval_execution_flow import EditingRuntime

from autocoding_agent.adapters.execution_scheduler import ExecutionScheduler, TaskAlreadyRunning
from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision, AgentStatus, RuntimeResult
from autocoding_agent.core.state_machine.models import TaskState


class WaitingRuntime:
    def __init__(self, wait=True):
        self.entered, self.release = Event(), Event()
        self.calls = 0
        if not wait:
            self.release.set()

    def run(self, turn):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(8)
        return RuntimeResult(
            decision=AgentDecision(status=AgentStatus.NEEDS_INPUT, message="请确认预期。"),
            runtime_session_id=turn.session_id,
        )


def test_independent_applications_overlap_and_reject_duplicate_task(tmp_path):
    settings = Settings(data_dir=tmp_path / "state", max_concurrent_tasks=2)
    first, second = WaitingRuntime(), WaitingRuntime()
    apps = [build_application(settings=settings, runtime=item) for item in (first, second)]
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
    identifier = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(apps[0].start, roots[0], "任务A", session_id=identifier)
        b = pool.submit(apps[1].start, roots[1], "任务B")
        try:
            assert first.entered.wait(5) and second.entered.wait(5)
            with pytest.raises(TaskAlreadyRunning):
                apps[1].send(identifier, "不能重复执行")
        finally:
            first.release.set()
            second.release.set()
        assert a.result(5).session_id != b.result(5).session_id
    assert first.calls == second.calls == 1


def test_queued_stop_keeps_input_and_resume_only_runs_once(tmp_path):
    settings = Settings(data_dir=tmp_path / "state", max_concurrent_tasks=1)
    first, second = WaitingRuntime(), WaitingRuntime(wait=False)
    app = build_application(settings=settings, runtime=first)
    other = build_application(settings=settings, runtime=second)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    queued = Event()
    identifier = str(uuid4())

    def progress(event):
        if event.phase.value == "queued":
            queued.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(app.start, tmp_path / "a", "先执行")
        assert first.entered.wait(5)
        waiting = pool.submit(other.start, tmp_path / "b", "排队需求不能丢失",
                              session_id=identifier, progress_sink=progress)
        try:
            assert queued.wait(5)
            other.request_stop(identifier)
            result = waiting.result(5)
            assert result.task_state == TaskState.PAUSED
            assert second.calls == 0
            session = other.get_session(identifier)
            assert any(message.content == "排队需求不能丢失" for message in session.messages)
            assert not session.runs
        finally:
            first.release.set()
        running.result(5)
    assert other._engine.scheduler.status("development", identifier)["state"] == "cancelled"
    other.resume(identifier)
    assert second.calls == 1


@pytest.mark.parametrize("first_write, second_write", [(True, False), (False, True), (True, True)])
def test_overlapping_directories_wait_but_independent_directory_runs(
    tmp_path, first_write, second_write,
):
    schedulers = [ExecutionScheduler(tmp_path / "state") for _ in range(3)]
    root = tmp_path / "repo"
    queued, entered, independent = Event(), Event(), Event()

    def waiting():
        with schedulers[1].acquire(
            "development", "b", root / "nested", write=second_write,
            progress_sink=lambda event: queued.set(),
        ):
            entered.set()

    def unrelated():
        with schedulers[2].acquire("development", "c", tmp_path / "different", write=True):
            independent.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with schedulers[0].acquire("development", "a", root, write=first_write):
            wait = pool.submit(waiting)
            other = pool.submit(unrelated)
            assert queued.wait(5) and independent.wait(5)
            assert not entered.is_set()
            other.result(5)
        wait.result(5)
    assert entered.is_set()


def test_same_directory_reads_overlap(tmp_path):
    scheduler = ExecutionScheduler(tmp_path / "state")
    entered = Event()

    def reader():
        with scheduler.acquire("incident", "b", tmp_path):
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with scheduler.acquire("development", "a", tmp_path):
            future = pool.submit(reader)
            assert entered.wait(5)
        future.result(5)


def test_approval_wait_rechecks_file_changes_before_model(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text("value=1", encoding="utf-8")
    runtime = EditingRuntime()
    app = build_application(settings=Settings(data_dir=tmp_path / "state"), runtime=runtime)
    pending = app.start(root, "修改service")
    blocker = ExecutionScheduler(tmp_path / "state")
    queued = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with blocker.acquire("git", "external-operation", root, write=True):
            future = pool.submit(app.approve, pending.session_id,
                                 progress_sink=lambda event: queued.set()
                                 if event.phase.value == "queued" else None)
            assert queued.wait(5)
            (root / "service.py").write_text("external=3", encoding="utf-8")
        result = future.result(5)
    assert result.task_state == TaskState.PAUSED
    assert len(runtime.turns) == 1
    assert "执行前文件依据发生变化" in result.message


def _process_wait(data, workspace, queued, entered):
    scheduler = ExecutionScheduler(data)
    with scheduler.acquire("development", "child", workspace, write=True,
                           progress_sink=lambda event: queued.set()):
        entered.set()


def test_resource_lock_crosses_process_boundary(tmp_path):
    context = multiprocessing.get_context("spawn")
    queued, entered = context.Event(), context.Event()
    scheduler = ExecutionScheduler(tmp_path / "state")
    process = context.Process(target=_process_wait,
                              args=(tmp_path / "state", tmp_path, queued, entered))
    try:
        with scheduler.acquire("development", "parent", tmp_path, write=True):
            process.start()
            assert queued.wait(8)
            assert not entered.is_set()
        process.join(8)
        assert process.exitcode == 0 and entered.is_set()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)


def test_equal_queue_timestamps_do_not_deadlock(tmp_path, monkeypatch):
    import autocoding_agent.adapters.execution_scheduler as module

    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    scheduler = ExecutionScheduler(tmp_path / "state", 1)
    signals = [Event(), Event()]

    def writer(index):
        with scheduler.acquire("development", str(index), tmp_path, write=True,
                               progress_sink=lambda event: signals[index].set()):
            return index

    with ThreadPoolExecutor(max_workers=2) as pool:
        with scheduler.acquire("development", "blocker", tmp_path, write=True):
            tasks = [pool.submit(writer, index) for index in range(2)]
            assert all(signal.wait(5) for signal in signals)
        assert {future.result(5) for future in tasks} == {0, 1}


def test_restart_releases_dead_queue_without_replaying_it(tmp_path):
    scheduler = ExecutionScheduler(tmp_path / "state")
    with closing(scheduler.database.connect()) as db, db:
        db.execute("INSERT INTO execution_slots "
                   "(request_id,workflow,task_id,owner_pid,state,workspace,"
                   "access,capacity,queued_at) "
                   "VALUES ('orphan','development','task',999999999,'queued',?,'write',1,?)",
                   (str(tmp_path), time.time()))
    restored = ExecutionScheduler(tmp_path / "state")
    assert restored.status("development", "task")["state"] == "interrupted"
    with restored.acquire("development", "task", tmp_path):
        assert restored.status("development", "task")["state"] == "running"
