"""HTTP 的用例服务；查询直接读取快照，执行由 Worker 调用现有应用门面。"""

from __future__ import annotations

from autocoding_agent.adapters.sqlite_incident_store import SQLiteIncidentStore
from autocoding_agent.adapters.sqlite_task_store import SQLiteTaskStore
from autocoding_agent.core.models import MessageRole
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.engine import format_incident_summary
from autocoding_api.config import ServerConfig
from autocoding_api.store import ApiError, QueueStore


class ApiService:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.queue = QueueStore(config.data_dir)
        # 查询服务不构建应用、不执行模型、不运行孤儿恢复扫描。
        self.sessions = {
            "development": SQLiteTaskStore(config.data_dir, migrate_legacy_json=False),
            "incident": SQLiteIncidentStore(config.data_dir, migrate_legacy_json=False),
        }

    def project(self, owner: str, project_id: str):
        if project_id not in self.config.users[owner].projects:
            raise ApiError(403, "无权访问此项目")
        return self.config.projects[project_id]

    def task(self, owner: str, task_id: str) -> dict:
        task = self.queue.task(task_id, owner)
        self.project(owner, task["project_id"])
        return task

    def session(self, task: dict):
        try:
            return self.sessions[task["workflow"]].load(task["id"])
        except KeyError:
            return None

    def create(self, owner: str, key: str, payload: dict) -> dict:
        project = self.project(owner, payload["project_id"])
        return self.queue.enqueue(
            owner=owner, key=key, operation="start", payload=payload,
            task={"workflow": payload["workflow"], "project_id": payload["project_id"],
                  "workspace": str(project.workspace),
                  "knowledge_project": project.knowledge_project},
        )

    def submit(self, owner: str, task_id: str, key: str, operation: str, payload: dict):
        task = self.task(owner, task_id)
        if old := self.queue.replay(owner, key, operation, task_id, payload):
            return old
        session = self.session(task)
        version = session.version if session else None
        if payload["expected_version"] != version:
            raise ApiError(409, "任务版本已变化，请重新查询后提交")
        if operation == "message":
            if session is None:
                raise ApiError(409, "任务尚未建立，请等待创建操作完成")
            if session.task_state in {TaskState.PAUSED, TaskState.RECOVERY_REQUIRED}:
                raise ApiError(409, "任务需要显式只读恢复")
            if session.task_state == TaskState.CANCELLED:
                raise ApiError(409, "任务已取消")
        else:
            latest = self.queue.latest_job(task_id)
            recovery_state = session and session.task_state in {
                TaskState.PAUSED, TaskState.RECOVERY_REQUIRED
            }
            if latest["status"] != "recovery_required" and not recovery_state:
                raise ApiError(409, "此任务不需要恢复")
        return self.queue.enqueue(
            owner=owner, key=key, operation=operation, payload=payload, task_id=task_id,
        )

    def get_job(self, owner: str, job_id: str) -> dict:
        job = self.queue.job(job_id, owner)
        self.task(owner, job["task_id"])
        return job

    def get_task(self, owner: str, task_id: str) -> dict:
        task = self.task(owner, task_id)
        job = self.queue.latest_job(task_id)
        session = self.session(task)
        decision = session.last_decision if session else None
        summary = None
        if decision:
            summary = (format_incident_summary(decision) if task["workflow"] == "incident"
                       else decision.message)
        # 不返回本机目录、运行原始日志、系统提示词、凭据或原始 SQL 行。
        result = None
        if decision:
            fields = ("assessment", "evidence", "next_actions", "approval", "changed_files",
                      "test_summary") if task["workflow"] == "development" else (
                "assessment", "page", "diagnosis", "findings", "recommended_actions", "question"
            )
            result = decision.model_dump(mode="json", include=set(fields))
        return {
            "task_id": task_id, "workflow": task["workflow"], "project_id": task["project_id"],
            "state": session.task_state.value if session else "created",
            "version": session.version if session else None,
            "job": job, "recovery_required": job["status"] == "recovery_required" or bool(
                session and session.task_state in {TaskState.PAUSED, TaskState.RECOVERY_REQUIRED}
            ),
            "summary": summary, "result": result,
            "messages": [{"role": item.role.value, "content": item.content}
                         for item in session.messages if item.role != MessageRole.SYSTEM][-100:]
            if session else [],
        }
