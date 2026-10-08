"""单执行进程：调用已有应用服务，保守恢复，绝不自动重放领取后的操作。"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from threading import Event, Thread

from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.git_version import GitTarget, GitVersionError
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.sqlserver_service import SQLServerConnectionService
from autocoding_api.config import ServerConfig
from autocoding_api.service import ApiService
from autocoding_api.store import ApiError

logger = logging.getLogger("autocoding_api.worker")


@contextmanager
def worker_lock(data_dir):
    """本机 OS 文件锁随进程退出释放；不可用时拒绝启动，不用超时猜测接管。"""
    directory = data_dir / "api"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("同一数据目录已有执行进程") from None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("同一数据目录已有执行进程") from None
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_services(config: ServerConfig):
    settings = Settings(data_dir=config.data_dir)
    git_targets = {
        str(project.workspace): GitTarget(
            project.workspace, project.git_remote, project.git_branch or ""
        )
        for project in config.projects.values() if project.git_remote
    }
    sql = SQLServerConnectionService(settings)
    state = sql.inspect()
    if state.config is not None and not state.configured:
        raise RuntimeError("数据库已配置但凭据不可用，请检查服务账户")
    reader = sql.reader()
    reference = reader.reference if reader is not None else None
    development = build_application(
        settings, database=reader, database_reference=reference, git_targets=git_targets,
    )
    # SQLite 的显式异常配置优先，与现有异常应用构建规则一致。
    incident = build_incident_application(
        settings, database=reader if settings.incident_sqlite_path is None else None,
        database_reference=reference if settings.incident_sqlite_path is None else None,
        git_targets=git_targets,
    )
    return {"development": development, "incident": incident}


class Worker:
    def __init__(self, config: ServerConfig, applications: dict) -> None:
        self.service = ApiService(config)
        self.applications = applications

    def run_once(self) -> bool:
        queue = self.service.queue
        job = queue.claim()
        if job is None:
            return False
        try:
            self._execute(job)
        except ApiError as exc:
            queue.finish(job["id"], "failed", exc.detail)
        except GitVersionError as exc:
            queue.finish(job["id"], "failed", str(exc))
        except (ValueError, KeyError) as exc:
            # 不把第三方错误文本、主机路径或连接字符串写入外部响应。
            logger.warning("job_rejected job_id=%s type=%s", job["id"], type(exc).__name__)
            session = self.service.session(job["task"])
            queue.finish(job["id"], "recovery_required" if session is not None else "failed",
                         "操作未能完成，请查询当前任务；版本可能已变化")
        except Exception as exc:
            logger.error("job_interrupted job_id=%s type=%s", job["id"], type(exc).__name__)
            queue.finish(job["id"], "recovery_required", "执行异常，请查询任务并选择只读恢复")
        else:
            queue.finish(job["id"], "succeeded")
        return True

    def reconcile_finished_commands(self) -> None:
        """领域命令回执已提交时，仅补齐队列确认，不重新执行代码或模型。"""
        for job in self.service.queue.running():
            if job["operation"] not in {"message", "approve", "reject", "remediate"}:
                continue
            try:
                task = self.service.queue.task(job["task_id"], job["owner"])
                session = self.service.session(task)
                if job["operation"] == "remediate":
                    if session is None:
                        _, intent = self.service.queue.original_operation(task["id"])
                        source_key = (
                            f"{intent['source_incident_id']}:{intent['source_cycle']}"
                        )
                        session = next(
                            (item for item in self.service.sessions["development"].list()
                             if item.source_incident_key == source_key), None,
                        )
                    if session is not None:
                        self.service.queue.bind_domain(task["id"], session.id)
                        if session.last_decision is not None:
                            self.service.queue.finish(job["id"], "succeeded")
                    continue
            except Exception:
                logger.warning("job_receipt_scan_failed job_id=%s", job["id"], exc_info=True)
                continue
            if session and any(
                receipt.command_id == job["id"] for receipt in session.command_receipts
            ):
                self.service.queue.finish(job["id"], "succeeded")

    def _execute(self, job: dict) -> None:
        task, payload = job["task"], job["payload"]
        config = self.service.config
        user = config.users.get(task["owner"])
        project = config.projects.get(task["project_id"])
        if not user or task["project_id"] not in user.projects or not project:
            raise ApiError(403, "项目权限已撤销")
        if str(project.workspace) != task["workspace"] or (
            project.knowledge_project != task["knowledge_project"]
        ):
            raise ApiError(409, "排队任务的项目配置已变化，请新建任务")
        application = self.applications[task["workflow"]]

        def sink(event):
            payload = event.model_dump(mode="json")
            payload["task_id"] = task["id"]
            self.service.queue.progress(job["id"], payload)

        session = self.service.session(task)
        if job["operation"] == "start":
            if session is not None:
                raise ValueError("此任务已建立，必须显式恢复")
            self._start(application, task, payload, sink)
            return
        if job["operation"] == "remediate":
            self._remediate(application, task, payload, sink)
            return
        if payload["expected_version"] != (session.version if session else None):
            raise ApiError(409, "执行前版本校验失败，请重新查询后提交")
        if job["operation"] == "git_sync":
            target = self.applications["development"].git(task["workspace"])
            if target is None:
                raise ApiError(409, "当前项目未配置 Git 远端")
            head = target.sync()
            self.service.queue.progress(job["id"], {"label": "代码已更新", "commit": head})
            return
        if job["operation"] == "git_publish":
            if task["workflow"] != "development":
                raise ApiError(409, "只有开发任务可以推送")
            published = application.git_publish(
                self.service.queue.domain_id(task["id"]),
                payload["summary"], payload["fingerprint"],
            )
            self.service.queue.progress(
                job["id"], {"label": "提交已推送", "commit": published["commit"]},
            )
            return
        domain_id = self.service.queue.domain_id(task["id"])
        if job["operation"] in {"approve", "reject"}:
            self.service.verify_approval(task, session, payload)
            if job["operation"] == "approve":
                application.approve(domain_id, command_id=job["id"], progress_sink=sink)
            else:
                application.reject(
                    domain_id, payload["reason"], command_id=job["id"], progress_sink=sink,
                )
            return
        if job["operation"] == "resume":
            if session is None:
                # 崩溃发生在领域会话建立前；用户显式恢复才允许开始原任务。
                initial_operation, original = self.service.queue.original_operation(task["id"])
                if initial_operation == "remediate":
                    self._remediate(application, task, original, sink)
                else:
                    self._start(application, task, original, sink)
            elif session.task_state in {TaskState.PAUSED, TaskState.RECOVERY_REQUIRED}:
                application.resume(domain_id, progress_sink=sink)
            else:
                # 会话落盘与队列完成之间也可能崩溃；重新只读核验，不重放旧输入。
                application.send(
                    domain_id,
                    "用户选择只读恢复：核对当前代码和已有证据，说明中断影响与安全下一步。"
                    "不假定旧操作已完成，不修改文件；需要修改时重新提出方案等待批准。",
                    command_id=job["id"], progress_sink=sink,
                )
        elif job["operation"] == "message":
            application.send(
                domain_id, payload["message"], command_id=job["id"], progress_sink=sink
            )
        else:
            raise ApiError(400, "不支持的操作")

    @staticmethod
    def _start(application, task, payload, sink):
        kwargs = {"session_id": task["id"], "project": task["knowledge_project"],
                  "progress_sink": sink}
        if task["workflow"] == "incident":
            kwargs.update(page_hint=payload.get("page_hint"), source="api",
                          external_reference=payload.get("external_reference"))
        application.start(task["workspace"], payload["message"], **kwargs)

    def _remediate(self, application, task, payload, sink) -> None:
        source = self.service.queue.task(payload["source_incident_id"], task["owner"])
        if (
            source["workflow"] != "incident" or source["project_id"] != task["project_id"]
            or source["workspace"] != task["workspace"]
        ):
            raise ApiError(409, "异常诊断任务与目标项目不匹配")
        incident = self.service.session(source)
        if (
            incident is None or incident.workspace != task["workspace"]
            or incident.version != payload["expected_version"]
            or incident.cycle_number != payload["source_cycle"]
            or not incident.can_remediate
        ):
            raise ApiError(409, "诊断轮次、版本或工作区已变化，请重新查询")
        outcome = application.start_incident_remediation(
            incident, session_id=task["id"], progress_sink=sink,
        )
        self.service.queue.bind_domain(task["id"], outcome.session_id)


def run_worker(config: ServerConfig, stop: Event | None = None) -> None:
    stopped = stop or Event()
    with worker_lock(config.data_dir):
        applications = build_services(config)
        worker = Worker(config, applications)
        worker.reconcile_finished_commands()
        worker.service.queue.recover_interrupted()
        heartbeats_done = Event()

        def pulse():
            while not heartbeats_done.is_set():
                try:
                    worker.service.queue.worker_heartbeat()
                except Exception:
                    logger.warning("worker_heartbeat_failed", exc_info=True)
                heartbeats_done.wait(5)

        heartbeat = Thread(target=pulse, name="api-worker-heartbeat", daemon=True)
        heartbeat.start()
        try:
            while not stopped.is_set():
                if not worker.run_once():
                    stopped.wait(0.5)
        finally:
            heartbeats_done.set()
            heartbeat.join(timeout=10)
