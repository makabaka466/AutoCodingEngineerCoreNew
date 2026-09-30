"""API 独立队列：先事务落盘再确认接收；领取后中断的操作不自动重放。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueStore:
    """短事务单连接；不在模型运行期间持有 SQLite 写锁。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "api" / "queue.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS api_tasks (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, workflow TEXT NOT NULL,
                    project_id TEXT NOT NULL, workspace TEXT NOT NULL,
                    knowledge_project TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES api_tasks(id),
                    owner TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    operation TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, progress TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(owner, key)
                );
                CREATE INDEX IF NOT EXISTS jobs_task ON jobs(task_id);
                CREATE TABLE IF NOT EXISTS service_state (
                    name TEXT PRIMARY KEY, updated_at TEXT NOT NULL
                );
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def enqueue(
        self, *, owner: str, key: str, operation: str, payload: dict,
        task: dict | None = None, task_id: str | None = None,
    ) -> dict:
        intent = json.dumps([operation, task_id, payload], sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(intent.encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute(
                "SELECT * FROM jobs WHERE owner=? AND key=?", (owner, key)
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ApiError(409, "此幂等键已用于另一操作，请使用新键")
                return self.public_job(dict(old))
            if task is not None:
                task_id = str(uuid4())
                db.execute(
                    "INSERT INTO api_tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (task_id, owner, task["workflow"], task["project_id"],
                     task["workspace"], task.get("knowledge_project"), now()),
                )
            else:
                self._task(db, task_id, owner)
                latest = db.execute(
                    "SELECT status FROM jobs WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if latest and latest["status"] in {"queued", "running"}:
                    raise ApiError(409, "此任务已有待执行或运行中的操作")
                if latest and latest["status"] == "recovery_required" and operation != "resume":
                    raise ApiError(409, "上次操作中断，请先显式恢复")
            job_id = str(uuid4())
            stamp = now()
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, ?, ?)",
                (job_id, task_id, owner, key, fingerprint, operation,
                 json.dumps(payload, ensure_ascii=False), stamp, stamp),
            )
            return self.public_job(dict(db.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()))

    def replay(self, owner: str, key: str, operation: str, task_id: str, payload: dict):
        """在读取已变化的领域版本之前处理重试，避免把合法重试误判为过期输入。"""
        with closing(self.connect()) as db:
            old = db.execute(
                "SELECT * FROM jobs WHERE owner=? AND key=?", (owner, key)
            ).fetchone()
        if old is None:
            return None
        intent = json.dumps([operation, task_id, payload], sort_keys=True, ensure_ascii=False)
        if old["fingerprint"] != hashlib.sha256(intent.encode()).hexdigest():
            raise ApiError(409, "此幂等键已用于另一操作，请使用新键")
        return self.public_job(dict(old))

    @staticmethod
    def _task(db, task_id: str | None, owner: str) -> dict:
        row = db.execute(
            "SELECT * FROM api_tasks WHERE id=? AND owner=?", (task_id, owner)
        ).fetchone()
        if not row:
            raise ApiError(404, "任务不存在")
        return dict(row)

    def task(self, task_id: str, owner: str) -> dict:
        with closing(self.connect()) as db:
            return self._task(db, task_id, owner)

    def job(self, job_id: str, owner: str) -> dict:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
        if not row:
            raise ApiError(404, "操作不存在")
        return self.public_job(dict(row))

    def latest_job(self, task_id: str) -> dict:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)
            ).fetchone()
        return self.public_job(dict(row))

    def original_payload(self, task_id: str) -> dict:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT payload FROM jobs WHERE task_id=? AND operation='start'", (task_id,)
            ).fetchone()
        return json.loads(row[0])

    def claim(self) -> dict | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET status='running', updated_at=? WHERE id=?", (now(), row["id"])
            )
            job = dict(row)
            job["payload"] = json.loads(job["payload"])
            job["task"] = self._task(db, job["task_id"], job["owner"])
            return job

    def finish(self, job_id: str, status: str, error: str | None = None) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=? AND status='running'",
                (status, error, now(), job_id),
            )

    def progress(self, job_id: str, progress: dict) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET progress=?, updated_at=? WHERE id=? AND status='running'",
                (json.dumps(progress, ensure_ascii=False), now(), job_id),
            )

    def recover_interrupted(self) -> None:
        """只在获得独占 Worker 锁后调用；排队中的操作保持原样。"""
        with self.transaction() as db:
            db.execute(
                "UPDATE jobs SET status='recovery_required', error=?, updated_at=? "
                "WHERE status='running'",
                ("执行进程中断；请查询已有结果并显式选择只读恢复", now()),
            )

    def worker_heartbeat(self) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO service_state(name, updated_at) VALUES ('worker', ?) "
                "ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at",
                (now(),),
            )

    def worker_ready(self) -> bool:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT updated_at FROM service_state WHERE name='worker'"
            ).fetchone()
        return bool(row and 0 <= (
            datetime.now(timezone.utc) - datetime.fromisoformat(row[0])
        ).total_seconds() <= 15)

    @staticmethod
    def public_job(job: dict) -> dict:
        result = {key: job[key] for key in (
            "id", "task_id", "operation", "status", "error", "created_at", "updated_at"
        )}
        result["job_id"] = result.pop("id")
        result["progress"] = json.loads(job["progress"]) if job["progress"] else None
        return result
