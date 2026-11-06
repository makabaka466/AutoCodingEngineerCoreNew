"""真实API队列与应用门面的并发、定向停止及退出回归。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread

from fastapi.testclient import TestClient
from test_http_api import OTHER_TOKEN, create, headers, server_config

from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision, AgentStatus, RuntimeResult
from autocoding_api.http import create_app
from autocoding_api.store import QueueStore
from autocoding_api.worker import Worker, run_worker


class ParallelRuntime:
    """模型只等待测试信号，验证任务确实重叠运行而不是只检查线程数。"""

    def __init__(self):
        self.lock = Lock()
        self.started = Event()
        self.release = Event()
        self.tasks = set()
        self.maximum = 0
        self.active = 0

    def run(self, turn):
        with self.lock:
            self.tasks.add(turn.session_id)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 2:
                self.started.set()
        try:
            assert self.release.wait(10), "测试未释放模拟模型"
            return RuntimeResult(
                decision=AgentDecision(status=AgentStatus.NEEDS_INPUT, message="请确认预期。"),
                runtime_session_id=turn.session_id,
            )
        finally:
            with self.lock:
                self.active -= 1


def test_queued_stop_is_durable_and_old_job_cannot_cancel_new_job(tmp_path):
    configured = server_config(tmp_path)
    client = TestClient(create_app(configured))
    job = create(client)
    path = f"/v1/tasks/{job['task_id']}/stop"
    assert client.post(path, headers=headers(token=OTHER_TOKEN),
                       json={"job_id": job["job_id"]}).status_code == 404
    stopped = client.post(path, headers=headers(), json={"job_id": job["job_id"]})
    assert stopped.status_code == 202
    assert stopped.json()["status"] == "cancelled"
    queue = QueueStore(configured.data_dir)
    assert queue.claim() is None
    resumed = client.post(
        f"/v1/tasks/{job['task_id']}/resume", headers=headers("resume-cancelled"),
        json={"expected_version": None},
    ).json()
    client.post(path, headers=headers(), json={"job_id": job["job_id"]})
    assert queue.job(resumed["job_id"], "alice")["status"] == "queued"
    assert queue.claim()["id"] == resumed["job_id"]


def test_two_jobs_overlap_and_stop_only_one_task(tmp_path):
    configured = server_config(tmp_path)
    runtime = ParallelRuntime()
    application = build_application(
        Settings(data_dir=configured.data_dir, hermes_skills_enabled=False), runtime=runtime,
    )
    worker = Worker(configured, {"development": application})
    client = TestClient(create_app(configured))
    first, second = create(client, key="a"), create(client, key="b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker.run_once) for _ in range(2)]
        try:
            assert runtime.started.wait(5)
            result = client.post(
                f"/v1/tasks/{first['task_id']}/stop", headers=headers(),
                json={"job_id": first["job_id"]},
            )
            assert result.json()["stop_requested"]
            view = client.get(f"/v1/tasks/{second['task_id']}", headers=headers()).json()
            assert view["execution"]["state"] == "running"
            assert "workspace" not in view["execution"]
        finally:
            runtime.release.set()
        assert all(future.result(timeout=5) for future in futures)
    assert runtime.maximum == 2
    assert application.get_session(first["task_id"]).task_state.value == "paused"
    assert application.get_session(second["task_id"]).task_state.value == "waiting_input"


def test_worker_exit_stops_active_jobs_and_leaves_unclaimed_queue(tmp_path, monkeypatch):
    configured = server_config(tmp_path)
    runtime = ParallelRuntime()
    application = build_application(
        Settings(data_dir=configured.data_dir, hermes_skills_enabled=False), runtime=runtime,
    )
    builds = []

    def build_once(config):
        builds.append(config)
        return {"development": application}

    monkeypatch.setattr("autocoding_api.worker.build_services", build_once)
    monkeypatch.setenv("AUTO_CODING_MAX_CONCURRENT_TASKS", "2")
    client = TestClient(create_app(configured))
    jobs = [create(client, key=f"job-{index}") for index in range(3)]
    stopped = Event()
    finished = Event()
    errors = []

    def run():
        try:
            run_worker(configured, stopped)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert runtime.started.wait(5)
        stopped.set()
        # 释放模型前先确认停止意图已持久化；写入安全结束后服务才可退出。
        database = application._engine.sessions.database
        for _ in range(100):
            if all(database.stop_requested(job["task_id"]) for job in jobs[:2]):
                break
            finished.wait(0.03)
        assert all(database.stop_requested(job["task_id"]) for job in jobs[:2])
        assert not finished.is_set()
    finally:
        stopped.set()
        runtime.release.set()
        thread.join(timeout=10)
    assert finished.is_set() and not errors
    assert len(builds) == 1
    assert runtime.maximum == 2
    assert len(runtime.tasks) == 2
    assert QueueStore(configured.data_dir).job(jobs[2]["job_id"], "alice")["status"] == "queued"


def test_stop_after_domain_result_does_not_poison_next_message(tmp_path):
    from test_agent_flows import ScriptedRuntime

    configured = server_config(tmp_path)
    client = TestClient(create_app(configured))
    runtime = ScriptedRuntime(*[
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="继续说明。") for _ in range(2)
    ])
    application = build_application(
        Settings(data_dir=configured.data_dir, hermes_skills_enabled=False), runtime=runtime,
    )
    worker = Worker(configured, {"development": application})
    job = create(client)
    execute = worker._execute

    def stop_after_domain_result(claimed):
        execute(claimed)
        worker.service.stop("alice", job["task_id"], job["job_id"])

    worker._execute = stop_after_domain_result
    worker.run_once()
    assert not application._engine.sessions.database.stop_requested(job["task_id"])
    worker._execute = execute
    view = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    reply = client.post(
        f"/v1/tasks/{job['task_id']}/messages", headers=headers("next-message"),
        json={"message": "补充条件。", "expected_version": view["version"]},
    )
    assert reply.status_code == 202
    worker.run_once()
    assert len(runtime.turns) == 2
    assert application.get_session(job["task_id"]).task_state.value == "waiting_input"
