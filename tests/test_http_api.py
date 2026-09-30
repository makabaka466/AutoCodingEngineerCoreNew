"""HTTP 到真实应用门面的回归；模型用脚本替代，不访问真实模型或业务数据库。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_agent_flows import ScriptedRuntime
from test_incident_flow import ScriptedStructuredRuntime

from autocoding_agent.application import build_application
from autocoding_agent.config import Settings
from autocoding_agent.core.models import (
    AgentDecision,
    AgentMode,
    AgentSession,
    AgentStatus,
    ApprovalRequest,
    ApprovalScope,
    ChangeProposal,
    ProposedChange,
)
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import IncidentDecision, IncidentSession, IncidentStatus
from autocoding_api.config import ServerConfig
from autocoding_api.http import create_app
from autocoding_api.service import ApiService
from autocoding_api.store import ApiError, QueueStore
from autocoding_api.worker import Worker, build_services, worker_lock

TOKEN = "a" * 40
OTHER_TOKEN = "b" * 40


@pytest.fixture
def configured(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ServerConfig.model_validate({
        "data_dir": str(tmp_path / "data"),
        "projects": {"demo": {"workspace": str(workspace)},
                     "private": {"workspace": str(workspace)}},
        "users": {"alice": {"token": TOKEN, "projects": ["demo"]},
                  "bob": {"token": OTHER_TOKEN, "projects": ["demo", "private"]}},
    })


def headers(key="request-1", token=TOKEN):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def create(client, *, workflow="development", key="request-1", project="demo"):
    response = client.post("/v1/tasks", headers=headers(key), json={
        "workflow": workflow, "project_id": project, "message": "请先调查异常",
    })
    assert response.status_code == 202, response.text
    return response.json()


def applications(config, *decisions, incident_decisions=()):
    settings = Settings(data_dir=config.data_dir, hermes_skills_enabled=False)
    runtime = ScriptedRuntime(*decisions)
    incident_runtime = ScriptedStructuredRuntime(incident_decisions)
    return {"development": build_application(settings, runtime=runtime),
            "incident": build_incident_application(settings, runtime=incident_runtime)}, runtime


def test_http_auth_scope_and_input_boundaries(configured):
    client = TestClient(create_app(configured))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    QueueStore(configured.data_dir).worker_heartbeat()
    assert client.get("/readyz").status_code == 200
    with sqlite3.connect(QueueStore(configured.data_dir).path) as db:
        db.execute("UPDATE service_state SET updated_at='2000-01-01T00:00:00+00:00'")
    assert client.get("/readyz").status_code == 503
    assert client.get("/v1/projects").status_code == 401
    assert client.get("/v1/projects", headers=headers(token="wrong")).status_code == 401
    response = client.get("/v1/projects", headers=headers())
    assert response.json() == {"projects": [{"project_id": "demo"}]}
    payload = {"workflow": "incident", "project_id": "private", "message": "问题"}
    assert client.post("/v1/tasks", headers=headers(), json=payload).status_code == 403
    payload["project_id"] = "demo"
    payload["workspace"] = "C:/Windows"
    assert client.post("/v1/tasks", headers=headers(), json=payload).status_code == 422
    del payload["workspace"]
    payload["message"] = " "
    assert client.post("/v1/tasks", headers=headers(), json=payload).status_code == 422
    payload["message"] = "x" * 20001
    assert client.post("/v1/tasks", headers=headers(), json=payload).status_code == 422
    assert client.get("/v1/tasks/not-a-uuid", headers=headers()).status_code == 422
    assert client.post("/v1/tasks", headers={"Authorization": f"Bearer {TOKEN}"},
                       json=payload).status_code == 422
    assert "/v1/tasks/{task_id}/messages" in client.get("/openapi.json").json()["paths"]


def test_task_and_job_are_owned_even_with_shared_project(configured):
    client = TestClient(create_app(configured))
    job = create(client)
    for path in (f"/v1/tasks/{job['task_id']}", f"/v1/jobs/{job['job_id']}"):
        assert client.get(path, headers=headers(token=OTHER_TOKEN)).status_code == 404
        assert client.get(path, headers=headers()).status_code == 200


def test_creation_is_atomic_durable_and_concurrent_retries_are_idempotent(configured):
    service = ApiService(configured)
    payload = {"workflow": "development", "project_id": "demo", "message": "调查"}
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda _: service.create("alice", "same", payload), range(12)))
    assert len({job["job_id"] for job in jobs}) == 1
    restarted = ApiService(configured)
    job = restarted.queue.claim()
    assert job["task_id"] == jobs[0]["task_id"]
    assert restarted.queue.claim() is None
    with pytest.raises(ApiError, match="幂等键"):
        service.create("alice", "same", {**payload, "message": "不同操作"})


def test_http_development_start_and_send_preserve_core_contract(configured):
    apps, runtime = applications(
        configured, AgentDecision(status=AgentStatus.NEEDS_INPUT, message="请说明预期"),
        AgentDecision(status=AgentStatus.COMPLETED, message="完成只读核验"),
    )
    client = TestClient(create_app(configured))
    job = create(client)
    worker = Worker(configured, apps)
    initial = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    assert initial["version"] is None and initial["job"]["status"] == "queued"
    assert runtime.turns == []
    assert worker.run_once()
    view = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    assert view["state"] == "waiting_input"
    assert view["summary"] == "请说明预期"
    assert view["job"]["progress"] is not None
    assert apps["development"].get_session(job["task_id"]).id == job["task_id"]
    assert str(configured.data_dir) not in json.dumps(view)
    path = f"/v1/tasks/{job['task_id']}/messages"
    payload = {"message": "预期信息", "expected_version": view["version"]}
    response = client.post(path, headers=headers("reply-1"), json=payload)
    assert response.status_code == 202
    assert client.post(path, headers=headers("reply-2"), json=payload).status_code == 409
    assert worker.run_once()
    # 已完成后重试旧版本的同一请求，仍返回原 job，不执行第二次。
    retry = client.post(path, headers=headers("reply-1"), json=payload)
    assert retry.json()["job_id"] == response.json()["job_id"]
    assert retry.json()["status"] == "succeeded"
    assert not worker.run_once()
    assert len(runtime.turns) == 2
    assert all(turn.mode == AgentMode.INSPECT for turn in runtime.turns)
    final = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    assert final["state"] == "completed"


def test_http_incident_uses_real_facade_and_returns_concise_result(configured):
    apps, _ = applications(configured, incident_decisions=[
        IncidentDecision(
            status=IncidentStatus.NEEDS_INPUT, message="需要页面名称", question="哪个页面？"
        )
    ])
    client = TestClient(create_app(configured))
    job = create(client, workflow="incident")
    assert Worker(configured, apps).run_once()
    session = apps["incident"].get_session(job["task_id"])
    assert session.source == "api"
    view = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    assert view["state"] == "waiting_input"
    assert view["result"]["question"] == "哪个页面？"
    assert "query_observations" not in view
    assert "runtime_session_id" not in view


def test_plain_messages_never_approve_changes(configured):
    proposal = ChangeProposal(
        summary="修复",
        changes=[ProposedChange(path="app.py", area="查询", current="旧", proposed="新")],
        expected_result="结果正确", impact=["仅查询"], validation=["核验"],
    )
    decision = AgentDecision(
        status=AgentStatus.APPROVAL_REQUIRED, message="请确认方案",
        approval=ApprovalRequest(scope=ApprovalScope.MODIFY, reason="需要修复", proposal=proposal),
    )
    apps, runtime = applications(configured, decision, decision)
    client = TestClient(create_app(configured))
    job = create(client)
    worker = Worker(configured, apps)
    worker.run_once()
    view = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    assert view["state"] == "waiting_modify_approval"
    assert view["result"]["approval"]["proposal"]["changes"][0]["path"] == "app.py"
    response = client.post(f"/v1/tasks/{job['task_id']}/messages", headers=headers("yes"), json={
        "message": "同意", "expected_version": view["version"],
    })
    assert response.status_code == 202
    worker.run_once()
    assert all(turn.mode == AgentMode.INSPECT for turn in runtime.turns)
    assert client.post(f"/v1/tasks/{job['task_id']}/approve", headers=headers()).status_code == 404


@pytest.mark.parametrize("with_session", [False, True])
def test_interrupted_claim_requires_explicit_recovery_and_keeps_queued_jobs(
    configured, with_session,
):
    apps, runtime = applications(
        configured, AgentDecision(status=AgentStatus.NEEDS_INPUT, message="中断前的结果"),
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="恢复后的核验"),
    )
    client = TestClient(create_app(configured))
    first = create(client)
    second = create(client, key="second")
    queue = QueueStore(configured.data_dir)
    claim = queue.claim()
    if with_session:
        # 模拟领域结果已保存、队列尚未确认时进程退出。
        Worker(configured, apps)._execute(claim)
    queue.recover_interrupted()
    worker = Worker(configured, apps)
    assert worker.run_once()  # 只执行未领取的 second
    assert worker.service.queue.job(second["job_id"], "alice")["status"] == "succeeded"
    assert not worker.run_once()
    view = client.get(f"/v1/tasks/{first['task_id']}", headers=headers()).json()
    assert view["recovery_required"]
    message = {"message": "继续", "expected_version": view["version"] or 0}
    assert client.post(f"/v1/tasks/{first['task_id']}/messages", headers=headers("continue"),
                       json=message).status_code == 409
    response = client.post(f"/v1/tasks/{first['task_id']}/resume", headers=headers("recover"),
                           json={"expected_version": view["version"]})
    assert response.status_code == 202, response.text
    assert worker.run_once()
    assert len(runtime.turns) == (3 if with_session else 2)
    assert all(turn.mode == AgentMode.INSPECT for turn in runtime.turns)


@pytest.mark.parametrize("workflow", ["development", "incident"])
def test_explicit_recovery_after_session_creation_before_runtime(configured, workflow):
    apps, _ = applications(
        configured, AgentDecision(status=AgentStatus.NEEDS_INPUT, message="重新核验"),
        incident_decisions=[
            IncidentDecision(
                status=IncidentStatus.NEEDS_INPUT,
                message="重新核验", question="请提供页面名称",
            )
        ],
    )
    client = TestClient(create_app(configured))
    job = create(client, workflow=workflow)
    queue = QueueStore(configured.data_dir)
    queue.claim()
    workspace = str(configured.projects["demo"].workspace)
    if workflow == "development":
        session = AgentSession(id=job["task_id"], workspace=workspace, goal="调查")
    else:
        session = IncidentSession(id=job["task_id"], workspace=workspace, problem="调查")
    ApiService(configured).sessions[workflow].create(session)
    queue.recover_interrupted()
    view = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    assert view["recovery_required"] and view["version"] == 0
    response = client.post(
        f"/v1/tasks/{job['task_id']}/resume", headers=headers("recovery"),
        json={"expected_version": 0},
    )
    assert response.status_code == 202, response.text
    Worker(configured, apps).run_once()
    assert queue.job(response.json()["job_id"], "alice")["status"] == "succeeded"
    assert client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()[
        "state"
    ] == "waiting_input"


def test_version_is_rechecked_before_execution(configured):
    apps, runtime = applications(configured, AgentDecision(
        status=AgentStatus.NEEDS_INPUT, message="补充信息"
    ))
    client = TestClient(create_app(configured))
    job = create(client)
    worker = Worker(configured, apps)
    worker.run_once()
    view = client.get(f"/v1/tasks/{job['task_id']}", headers=headers()).json()
    response = client.post(f"/v1/tasks/{job['task_id']}/messages", headers=headers("reply"), json={
        "message": "新消息", "expected_version": view["version"],
    })
    assert response.status_code == 202
    store = worker.service.sessions["development"]
    session = store.load(job["task_id"])
    session.version += 1
    store.save(session)
    worker.run_once()
    assert worker.service.queue.job(response.json()["job_id"], "alice")["status"] == "failed"
    assert len(runtime.turns) == 1


def test_worker_error_is_sanitized_and_not_automatically_retried(configured):
    class BrokenApplication:
        def start(self, *args, **kwargs):
            raise RuntimeError("password=super-secret C:/private/key")

    service = ApiService(configured)
    job = service.create("alice", "one", {
        "workflow": "development", "project_id": "demo", "message": "调查",
    })
    worker = Worker(configured, {"development": BrokenApplication()})
    assert worker.run_once()
    result = service.queue.job(job["job_id"], "alice")
    assert result["status"] == "recovery_required"
    assert "super-secret" not in json.dumps(result)
    assert not worker.run_once()


def test_only_one_worker_process_can_hold_lock(configured):
    script = (
        "import sys; from pathlib import Path; from autocoding_api.worker import worker_lock\n"
        "try:\n with worker_lock(Path(sys.argv[1])): pass\n"
        "except RuntimeError: sys.exit(42)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    with worker_lock(configured.data_dir):
        blocked = subprocess.run([sys.executable, "-c", script, str(configured.data_dir)],
                                 env=env, capture_output=True, timeout=20)
        assert blocked.returncode == 42, blocked.stderr
    released = subprocess.run([sys.executable, "-c", script, str(configured.data_dir)],
                              env=env, capture_output=True, timeout=20)
    assert released.returncode == 0, released.stderr


@pytest.mark.parametrize(
    "change", ["short_token", "duplicate_token", "unknown_project", "relative", "placeholder"]
)
def test_invalid_server_config_is_rejected(configured, change):
    data = configured.model_dump(mode="json")
    # SecretStr 的导出掩码不是原始 Token。
    data["users"]["alice"]["token"] = TOKEN
    data["users"]["bob"]["token"] = OTHER_TOKEN
    if change == "short_token":
        data["users"]["alice"]["token"] = "short"
    elif change == "duplicate_token":
        data["users"]["bob"]["token"] = TOKEN
    elif change == "unknown_project":
        data["users"]["alice"]["projects"] = ["missing"]
    elif change == "placeholder":
        data["users"]["alice"]["token"] = "REPLACE_WITH_REAL_TOKEN_OF_AT_LEAST_32_CHARS"
    else:
        data["data_dir"] = "relative"
    with pytest.raises(ValidationError):
        ServerConfig.model_validate(data)


def test_worker_bootstrap_without_database_does_not_call_model(configured, monkeypatch):
    monkeypatch.setenv("AUTO_CODING_HERMES_SKILLS_ENABLED", "false")
    monkeypatch.delenv("AUTO_CODING_INCIDENT_SQLITE_PATH", raising=False)
    services = build_services(configured)
    assert set(services) == {"development", "incident"}
    assert services["development"].list_sessions() == []
    assert services["incident"].list_sessions() == []
