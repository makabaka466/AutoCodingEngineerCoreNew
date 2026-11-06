"""同一数据目录内的跨进程任务调度；排队不占模型轮次，目录读写按实际路径协调。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from functools import wraps
from inspect import signature
from pathlib import Path
from threading import local
from uuid import uuid4

from autocoding_agent.adapters.sqlite_runtime import DEVELOPMENT_LAYOUT, SQLiteRuntimeDatabase
from autocoding_agent.core.progress import (
    ProgressEvent,
    ProgressPhase,
    ProgressWorkflow,
    emit_progress,
)
from autocoding_agent.core.recovery.scanner import _pid_is_alive


class TaskAlreadyRunning(ValueError):
    """同一任务已由其他窗口、线程或进程处理，不能重复执行。"""


class QueuedTaskStopped(RuntimeError):
    """尚未取得执行资源的任务已被用户停止。"""


def exclusive_task(method):
    """在读取/修改会话前独占任务；同线程resume调用cancel允许复用占用。"""
    @wraps(method)
    def wrapped(engine, *args, **kwargs):
        scheduler = getattr(engine, "scheduler", None)
        if scheduler is None:
            return method(engine, *args, **kwargs)
        if method.__name__ == "start":
            task_id = kwargs.setdefault("session_id", None) or str(uuid4())
            kwargs["session_id"] = task_id
        else:
            task_id = args[0] if args else kwargs["session_id"]
        workflow = "incident" if engine.sessions.database.layout.tasks == "incident_tasks" else (
            "development"
        )
        with scheduler.reserve(workflow, task_id):
            return method(engine, *args, **kwargs)
    return wrapped


def scheduled_turn(method):
    """先保存本轮输入，再等待资源；恢复时不丢排队输入，也不重放未知副作用。"""
    parameters = signature(method)

    @wraps(method)
    def wrapped(engine, *args, **kwargs):
        scheduler = getattr(engine, "scheduler", None)
        if scheduler is None:
            return method(engine, *args, **kwargs)
        from autocoding_agent.core.models import AgentEvent, ChatMessage, EventType, MessageRole
        from autocoding_agent.core.state_machine.models import TaskState

        values = parameters.bind(engine, *args, **kwargs).arguments
        session, command = values["session"], values["command"]
        mode = values.get("mode")
        mode = mode.value if mode is not None else "inspect"
        workflow = "development" if "mode" in values else "incident"
        engine.state_machine.transition(
            session, {"inspect": TaskState.INSPECTING, "implement": TaskState.IMPLEMENTING,
                      "verify": TaskState.VERIFYING}[mode],
            reason="本轮输入已保存，正在等待执行资源。", command_id=command.id,
            expected_version=command.expected_version,
        )
        command.expected_version = session.version
        session.messages.append(ChatMessage(
            role=MessageRole.USER, content=values["user_message"],
            attachments=values.get("attachments") or [],
        ))
        session.events.append(AgentEvent(
            type=EventType.TURN_STARTED, message="本轮输入已持久化，准备调度。",
            command_id=command.id,
            data={"mode": mode, "owner_id": engine.runtime_lifecycle.owner_id,
                  "owner_pid": os.getpid(), "scheduled": True},
        ))
        engine.sessions.save(session)
        try:
            with scheduler.acquire(
                workflow, session.id, session.workspace, write=mode != "inspect",
                progress_sink=values.get("progress_sink"),
                stop_requested=lambda: engine._stop_requested(session.id),
            ):
                return method(engine, *args, **kwargs)
        except QueuedTaskStopped as exc:
            return engine._pause_at_boundary(session, command, message=str(exc))
    return wrapped


class ExecutionScheduler:
    """SQLite只用于短事务分配资源；模型、文件操作和等待均在事务外执行。"""

    def __init__(self, data_dir: str | Path, max_concurrent_tasks: int = 3):
        if not 1 <= max_concurrent_tasks <= 16:
            raise ValueError("并发任务数必须在1到16之间。")
        self.limit = max_concurrent_tasks
        self.database = SQLiteRuntimeDatabase(data_dir, DEVELOPMENT_LAYOUT)
        self._local = local()
        self.database.initialize("""
            CREATE TABLE IF NOT EXISTS execution_slots (
                request_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, task_id TEXT NOT NULL,
                owner_pid INTEGER NOT NULL, state TEXT NOT NULL, workspace TEXT NOT NULL DEFAULT '',
                access TEXT NOT NULL DEFAULT 'read', capacity INTEGER NOT NULL,
                queued_at REAL NOT NULL, started_at REAL, finished_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS execution_active_task
            ON execution_slots(workflow, task_id)
            WHERE state IN ('reserved','queued','running','stopping');
            CREATE INDEX IF NOT EXISTS execution_active_state ON execution_slots(state, queued_at);
        """)
        with closing(self.database.connect()) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._clear_dead(db)

    def _clear_dead(self, db):
        alive = {}
        rows = db.execute("SELECT * FROM execution_slots WHERE state IN "
                          "('reserved','queued','running','stopping')").fetchall()
        for row in rows:
            pid = row["owner_pid"]
            if pid not in alive:
                alive[pid] = _pid_is_alive(pid)
            if alive[pid]:
                continue
            table = {"development": "runs", "incident": "incident_runs"}.get(row["workflow"])
            # 主进程已退出但Claude子进程仍在写入时，不能释放共享目录。
            if table and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                    (table,)).fetchone():
                runs = db.execute(f"SELECT run_json FROM {table} WHERE task_id=? "
                                  "ORDER BY started_at DESC LIMIT 1", (row["task_id"],)).fetchall()
                child = json.loads(runs[0][0]).get("runtime_pid") if runs else None
                if child and _pid_is_alive(child):
                    continue
            db.execute("UPDATE execution_slots SET state='interrupted',finished_at=? "
                       "WHERE request_id=?", (time.time(), row["request_id"]))

    @contextmanager
    def reserve(self, workflow: str, task_id: str):
        key = (workflow, task_id)
        owned = getattr(self._local, "owned", None)
        if owned is None:
            owned = self._local.owned = {}
        if key in owned:
            yield owned[key]
            return
        request_id = str(uuid4())
        with closing(self.database.connect()) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._clear_dead(db)
            try:
                db.execute("INSERT INTO execution_slots "
                           "(request_id,workflow,task_id,owner_pid,state,capacity,queued_at) "
                           "VALUES (?,?,?,?,'reserved',?,?)",
                           (request_id, workflow, task_id, os.getpid(), self.limit, time.time()))
            except sqlite3.IntegrityError as exc:
                raise TaskAlreadyRunning("该任务正在其他窗口执行或排队，请勿重复操作。") from exc
        owned[key] = request_id
        try:
            yield request_id
        finally:
            owned.pop(key, None)
            with closing(self.database.connect()) as db, db:
                db.execute("UPDATE execution_slots SET state=CASE WHEN state='stopping' "
                           "THEN 'cancelled' ELSE 'finished' END,finished_at=? "
                           "WHERE request_id=? AND state IN "
                           "('reserved','queued','running','stopping')",
                           (time.time(), request_id))

    @contextmanager
    def acquire(self, workflow: str, task_id: str, workspace: str | Path, *,
                write: bool = False, progress_sink=None, stop_requested=None):
        root = os.path.normcase(str(Path(workspace).resolve()))
        with self.reserve(workflow, task_id) as request_id:
            with closing(self.database.connect()) as db, db:
                db.execute("UPDATE execution_slots SET state='queued',workspace=?,access=? "
                           "WHERE request_id=?", (root, "write" if write else "read", request_id))
            notified = False
            while True:
                if stop_requested and stop_requested():
                    with closing(self.database.connect()) as db, db:
                        db.execute("UPDATE execution_slots SET state='stopping',finished_at=? "
                                   "WHERE request_id=?", (time.time(), request_id))
                    raise QueuedTaskStopped("排队任务已停止，尚未开始本轮模型执行。")
                with closing(self.database.connect()) as db, db:
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    self._clear_dead(db)
                    rows = db.execute(
                        "SELECT * FROM execution_slots WHERE state IN "
                        "('queued','running') ORDER BY queued_at,request_id",
                    ).fetchall()
                    own = next(item for item in rows if item["request_id"] == request_id)
                    running = [item for item in rows if item["state"] == "running"]
                    capacity = min([self.limit, *[item["capacity"] for item in rows]])
                    blocked = False
                    for item in rows:
                        if item["request_id"] == request_id:
                            continue
                        # 让等待中的写任务也有机会；不阻塞其他独立目录。
                        if item["state"] != "running" and (
                            item["queued_at"], item["request_id"]
                        ) > (own["queued_at"], own["request_id"]):
                            continue
                        other = Path(item["workspace"])
                        overlaps = Path(root).is_relative_to(other) or other.is_relative_to(root)
                        if overlaps and (write or item["access"] == "write"):
                            blocked = True
                            break
                    if len(running) < capacity and not blocked:
                        db.execute("UPDATE execution_slots SET state='running',started_at=? "
                                   "WHERE request_id=?", (time.time(), request_id))
                        break
                if not notified:
                    emit_progress(progress_sink, ProgressEvent.for_phase(
                        ProgressWorkflow.INCIDENT if workflow == "incident" else (
                            ProgressWorkflow.DEVELOPMENT), ProgressPhase.QUEUED,
                        task_id=task_id, detail="等待执行名额或共享目录释放，可停止当前排队任务。",
                    ))
                    notified = True
                time.sleep(0.1)
            try:
                yield
            finally:
                with closing(self.database.connect()) as db, db:
                    db.execute("UPDATE execution_slots SET state='reserved',finished_at=? "
                               "WHERE request_id=?", (time.time(), request_id))

    def status(self, workflow: str, task_id: str) -> dict | None:
        with closing(self.database.connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM execution_slots WHERE workflow=? AND task_id=? "
                             "ORDER BY queued_at DESC LIMIT 1", (workflow, task_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        now = result["finished_at"] or time.time()
        result["queue_seconds"] = max(0, (result["started_at"] or now) - result["queued_at"])
        result["execution_seconds"] = (
            max(0, now - result["started_at"]) if result["started_at"] else 0
        )
        return result
