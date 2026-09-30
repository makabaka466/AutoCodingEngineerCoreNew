"""远程审批与诊断交接的真实应用门面契约，不触发外部模型或真实文件修改。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_agent_flows import _approval, _completed, _diagnosed_incident
from test_http_api import applications, create, headers, server_config

from autocoding_agent.core.models import AgentDecision, AgentMode, AgentStatus, ApprovalScope
from autocoding_api.http import create_app
from autocoding_api.store import QueueStore
from autocoding_api.worker import Worker


@pytest.fixture
def configured(tmp_path):
    return server_config(tmp_path)


def diagnosed_source(config, client):
    job = create(client, workflow="incident")
    queue = QueueStore(config.data_dir)
    assert queue.claim()["id"] == job["job_id"]
    session = _diagnosed_incident(config.projects["demo"].workspace).model_copy(
        update={"id": job["task_id"]}
    )
    # 已完成的真实领域快照由原有 Store 保存；诊断引擎证据门槛由原有测试覆盖。
    from autocoding_api.service import ApiService

    ApiService(config).sessions["incident"].create(session)
    queue.finish(job["job_id"], "succeeded")
    return job


def approval_payload(view):
    return {"expected_version": view["version"],
            "approval_id": view["pending_approval"]["approval_id"],
            "scope": view["pending_approval"]["scope"]}


def test_diagnosis_to_review_modify_verify_is_remote_and_restartable(configured):
    client = TestClient(create_app(configured))
    source = diagnosed_source(configured, client)
    source_path = f"/v1/tasks/{source['task_id']}"
    diagnosed = client.get(source_path, headers=headers()).json()
    assert diagnosed["can_remediate"]
    handoff_path = source_path + "/remediation"
    handoff = {"expected_version": diagnosed["version"],
               "cycle_number": diagnosed["cycle_number"]}
    submitted = client.post(handoff_path, headers=headers("handoff-1"), json=handoff)
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert job["task_id"] != source["task_id"]
    assert client.post(handoff_path, headers=headers("handoff-1"), json=handoff).json() == job
    assert client.post(handoff_path, headers=headers("handoff-2"), json=handoff).json() == job
    assert client.post(handoff_path, headers=headers("handoff-2"), json={
        **handoff, "expected_version": handoff["expected_version"] + 1
    }).status_code == 409

    apps, first_runtime = applications(configured, _approval(ApprovalScope.MODIFY))
    worker = Worker(configured, apps)
    assert worker.run_once()
    repair_path = f"/v1/tasks/{job['task_id']}"
    review = client.get(repair_path, headers=headers()).json()
    assert review["state"] == "waiting_modify_approval"
    assert client.post(
        repair_path + "/messages", headers=headers("handoff-2"),
        json={"message": "换个问题", "expected_version": review["version"]},
    ).status_code == 409
    assert review["pending_approval"]["proposal"]["changes"][0]["path"]
    assert review["pending_approval"]["proposal"]["changes"][0]["current"]
    assert review["pending_approval"]["proposal"]["changes"][0]["proposed"]
    assert review["pending_approval"]["proposal"]["impact"]
    assert review["pending_approval"]["proposal"]["validation"]
    assert apps["development"].get_session(job["task_id"]).source_incident_key == (
        f"{source['task_id']}:{handoff['cycle_number']}"
    )
    assert first_runtime.turns[0].mode == AgentMode.INSPECT
    assert not list(configured.projects["demo"].workspace.iterdir())

    # API、Worker 均重建后，从持久化方案继续审批。
    next_apps, next_runtime = applications(
        configured, _approval(ApprovalScope.VERIFY), _completed()
    )
    restarted = TestClient(create_app(configured))
    review = restarted.get(repair_path, headers=headers()).json()
    modify = approval_payload(review)
    for index, changed in enumerate((
        {**modify, "expected_version": modify["expected_version"] + 1},
        {**modify, "scope": "verify"},
        {**modify, "approval_id": "0" * 64},
    )):
        assert restarted.post(
            repair_path + "/approve", headers=headers(f"bad-{index}"), json=changed
        ).status_code == 409
    accepted = restarted.post(repair_path + "/approve", headers=headers("approve-modify"),
                              json=modify)
    assert accepted.status_code == 202
    assert restarted.post(repair_path + "/approve", headers=headers("approve-modify"),
                          json=modify).json()["job_id"] == accepted.json()["job_id"]
    assert restarted.post(repair_path + "/approve", headers=headers("different-key"),
                          json=modify).status_code == 409
    second_worker = Worker(configured, next_apps)
    assert second_worker.run_once()
    assert next_runtime.turns[0].mode == AgentMode.IMPLEMENT
    assert restarted.post(repair_path + "/approve", headers=headers("approve-modify"),
                          json=modify).json()["status"] == "succeeded"
    verifying = restarted.get(repair_path, headers=headers()).json()
    assert verifying["state"] == "waiting_verify_approval"
    assert verifying["pending_approval"]["scope"] == "verify"
    assert verifying["pending_approval"]["approval_id"] != modify["approval_id"]
    stale = {**modify, "expected_version": verifying["version"]}
    assert restarted.post(repair_path + "/approve", headers=headers("stale"),
                          json=stale).status_code == 409
    approved_verify = restarted.post(repair_path + "/approve", headers=headers("approve-verify"),
                                     json=approval_payload(verifying))
    assert approved_verify.status_code == 202
    assert second_worker.run_once()
    done = restarted.get(repair_path, headers=headers()).json()
    assert done["state"] == "completed"
    assert done["pending_approval"] is None
    assert [turn.mode for turn in next_runtime.turns] == [AgentMode.IMPLEMENT, AgentMode.VERIFY]
    assert restarted.get(source_path, headers=headers()).json()["state"] == "completed"


def test_rejection_stays_read_only_and_does_not_reuse_approval(configured):
    apps, runtime = applications(
        configured, _approval(ApprovalScope.MODIFY),
        AgentDecision(status=AgentStatus.NEEDS_INPUT, message="只读说明"),
    )
    client = TestClient(create_app(configured))
    job = create(client)
    worker = Worker(configured, apps)
    worker.run_once()
    path = f"/v1/tasks/{job['task_id']}"
    view = client.get(path, headers=headers()).json()
    rejection = {**approval_payload(view), "reason": "先不要改代码"}
    response = client.post(path + "/reject", headers=headers("reject-1"), json=rejection)
    assert response.status_code == 202
    assert worker.run_once()
    assert client.get(path, headers=headers()).json()["pending_approval"] is None
    assert [turn.mode for turn in runtime.turns] == [AgentMode.INSPECT, AgentMode.INSPECT]
    assert "先不要改代码" in runtime.turns[-1].user_message
    assert client.post(path + "/reject", headers=headers("reject-1"),
                       json=rejection).json()["job_id"] == response.json()["job_id"]
    assert client.post(path + "/approve", headers=headers("later"),
                       json=approval_payload(view)).status_code == 409


def test_approval_is_rechecked_when_saved_proposal_changes_before_worker(configured):
    apps, runtime = applications(configured, _approval(ApprovalScope.MODIFY))
    client = TestClient(create_app(configured))
    job = create(client)
    worker = Worker(configured, apps)
    worker.run_once()
    path = f"/v1/tasks/{job['task_id']}"
    view = client.get(path, headers=headers()).json()
    response = client.post(path + "/approve", headers=headers("approve"),
                           json=approval_payload(view))
    assert response.status_code == 202
    saved = worker.service.sessions["development"].load(job["task_id"])
    saved.pending_approval.proposal.changes[0].proposed = "另一项未审阅修改"
    worker.service.sessions["development"].save(saved)
    assert worker.run_once()
    assert worker.service.queue.job(response.json()["job_id"], "alice")["status"] == "failed"
    assert [turn.mode for turn in runtime.turns] == [AgentMode.INSPECT]


def test_saved_progress_events_are_cursor_based_and_owner_scoped(configured):
    apps, _ = applications(configured, AgentDecision(
        status=AgentStatus.NEEDS_INPUT, message="请补充页面"
    ))
    client = TestClient(create_app(configured))
    job = create(client)
    worker = Worker(configured, apps)
    worker.run_once()
    path = f"/v1/tasks/{job['task_id']}/events"
    assert client.get(path, headers=headers(token="b" * 40)).status_code == 404
    assert client.get(path + "?after=-1", headers=headers()).status_code == 422
    assert client.get(path + "?limit=101", headers=headers()).status_code == 422
    page = client.get(path + "?limit=2", headers=headers()).json()
    assert len(page["events"]) == 2
    assert [event["kind"] for event in page["events"]] == ["job_queued", "job_started"]
    restarted = TestClient(create_app(configured))
    later = restarted.get(path + f"?after={page['next_cursor']}", headers=headers()).json()
    assert later["events"] and later["events"][-1]["data"]["status"] == "succeeded"
    assert any(item["kind"] == "progress" for item in later["events"])
    assert all(item["id"] > page["next_cursor"] for item in later["events"])
    assert restarted.get(path + f"?after={later['next_cursor']}",
                         headers=headers()).json() == {
        "events": [], "next_cursor": later["next_cursor"]
    }
    assert "system_prompt" not in json.dumps(later)


def test_remediation_source_version_is_checked_again_before_execution(configured):
    client = TestClient(create_app(configured))
    source = diagnosed_source(configured, client)
    path = f"/v1/tasks/{source['task_id']}"
    view = client.get(path, headers=headers()).json()
    intent = {"expected_version": view["version"], "cycle_number": view["cycle_number"]}
    response = client.post(path + "/remediation", headers=headers("remediate"), json=intent)
    assert response.status_code == 202
    apps, runtime = applications(configured, _approval(ApprovalScope.MODIFY))
    session = apps["incident"].get_session(source["task_id"])
    session.version += 1
    apps["incident"]._engine.sessions.save(session)
    worker = Worker(configured, apps)
    assert worker.run_once()
    assert worker.service.queue.job(response.json()["job_id"], "alice")["status"] == "failed"
    assert not runtime.turns
    repair = worker.service.queue.task(response.json()["task_id"], "alice")
    assert worker.service.session(repair) is None


def test_same_incident_cycle_cannot_silently_reuse_a_different_diagnosis_version(configured):
    client = TestClient(create_app(configured))
    source = diagnosed_source(configured, client)
    path = f"/v1/tasks/{source['task_id']}"
    view = client.get(path, headers=headers()).json()
    original = {"expected_version": view["version"], "cycle_number": view["cycle_number"]}
    first = client.post(path + "/remediation", headers=headers("old"), json=original)
    assert first.status_code == 202
    apps, runtime = applications(configured, _approval(ApprovalScope.MODIFY))
    session = apps["incident"].get_session(source["task_id"])
    session.version += 1
    apps["incident"]._engine.sessions.save(session)
    updated = client.get(path, headers=headers()).json()
    assert updated["can_remediate"] and updated["cycle_number"] == view["cycle_number"]
    fresh = {"expected_version": updated["version"],
             "cycle_number": updated["cycle_number"]}
    assert client.post(path + "/remediation", headers=headers("new"), json=fresh).status_code == 409
    worker = Worker(configured, apps)
    assert worker.run_once()  # 旧操作无法基于变化后的诊断自动执行。
    retry = client.post(path + "/remediation", headers=headers("retry"), json=fresh)
    assert retry.status_code == 202
    assert retry.json()["task_id"] == first.json()["task_id"]
    assert worker.run_once()
    assert len(runtime.turns) == 1


def test_reconfigured_project_cannot_read_or_execute_existing_task(configured):
    client = TestClient(create_app(configured))
    job = create(client)
    moved = configured.data_dir.parent / "another-workspace"
    moved.mkdir()
    configured.projects["demo"].workspace = moved
    for endpoint in (f"/v1/tasks/{job['task_id']}", f"/v1/jobs/{job['job_id']}",
                     f"/v1/tasks/{job['task_id']}/events"):
        assert client.get(endpoint, headers=headers()).status_code == 403
    apps, runtime = applications(configured, _approval(ApprovalScope.MODIFY))
    assert Worker(configured, apps).run_once()
    assert not runtime.turns


def test_remediation_recovers_before_domain_session_was_created(configured):
    client = TestClient(create_app(configured))
    source = diagnosed_source(configured, client)
    view = client.get(f"/v1/tasks/{source['task_id']}", headers=headers()).json()
    response = client.post(
        f"/v1/tasks/{source['task_id']}/remediation", headers=headers("handoff"),
        json={"expected_version": view["version"], "cycle_number": view["cycle_number"]},
    )
    assert response.status_code == 202
    repair_id = response.json()["task_id"]
    queue = QueueStore(configured.data_dir)
    assert queue.claim()["id"] == response.json()["job_id"]
    queue.recover_interrupted()
    apps, runtime = applications(configured, _approval(ApprovalScope.MODIFY))
    recovery = client.post(
        f"/v1/tasks/{repair_id}/resume", headers=headers("recovery"),
        json={"expected_version": None},
    )
    assert recovery.status_code == 202
    assert Worker(configured, apps).run_once()
    assert client.get(f"/v1/tasks/{repair_id}", headers=headers()).json()[
        "state"
    ] == "waiting_modify_approval"
    assert len(runtime.turns) == 1 and runtime.turns[0].mode == AgentMode.INSPECT


def test_remediation_reuses_existing_domain_session_with_api_binding(configured):
    client = TestClient(create_app(configured))
    source = diagnosed_source(configured, client)
    apps, runtime = applications(
        configured, _approval(ApprovalScope.MODIFY), _approval(ApprovalScope.VERIFY)
    )
    incident = apps["incident"].get_session(source["task_id"])
    existing = apps["development"].start_incident_remediation(incident)
    assert existing.session_id != source["task_id"]
    view = client.get(f"/v1/tasks/{source['task_id']}", headers=headers()).json()
    response = client.post(
        f"/v1/tasks/{source['task_id']}/remediation", headers=headers("handoff"),
        json={"expected_version": view["version"], "cycle_number": view["cycle_number"]},
    )
    assert response.status_code == 202
    worker = Worker(configured, apps)
    assert worker.run_once()
    repair_id = response.json()["task_id"]
    assert worker.service.queue.domain_id(repair_id) == existing.session_id
    repair_path = f"/v1/tasks/{repair_id}"
    review = client.get(repair_path, headers=headers()).json()
    assert review["state"] == "waiting_modify_approval"
    assert len(runtime.turns) == 1
    approved = client.post(repair_path + "/approve", headers=headers("approved"),
                           json=approval_payload(review))
    assert approved.status_code == 202
    assert worker.run_once()
    assert [turn.mode for turn in runtime.turns] == [AgentMode.INSPECT, AgentMode.IMPLEMENT]
    assert client.get(repair_path, headers=headers()).json()["state"] == "waiting_verify_approval"


def test_restarted_worker_recognizes_approved_command_receipt(configured):
    apps, runtime = applications(
        configured, _approval(ApprovalScope.MODIFY), _approval(ApprovalScope.VERIFY)
    )
    client = TestClient(create_app(configured))
    job = create(client)
    worker = Worker(configured, apps)
    worker.run_once()
    path = f"/v1/tasks/{job['task_id']}"
    view = client.get(path, headers=headers()).json()
    approval = client.post(path + "/approve", headers=headers("approve"),
                           json=approval_payload(view)).json()
    claimed = worker.service.queue.claim()
    assert claimed["id"] == approval["job_id"]
    worker._execute(claimed)  # 领域回执已保存，模拟队列 finish 之前进程退出。
    restarted = Worker(configured, apps)
    restarted.reconcile_finished_commands()
    restarted.service.queue.recover_interrupted()
    assert restarted.service.queue.job(approval["job_id"], "alice")["status"] == "succeeded"
    assert [turn.mode for turn in runtime.turns] == [AgentMode.INSPECT, AgentMode.IMPLEMENT]
    assert not restarted.run_once()


def test_restarted_worker_recognizes_saved_remediation_without_rerunning(configured):
    client = TestClient(create_app(configured))
    source = diagnosed_source(configured, client)
    view = client.get(f"/v1/tasks/{source['task_id']}", headers=headers()).json()
    handoff = client.post(
        f"/v1/tasks/{source['task_id']}/remediation", headers=headers("handoff"),
        json={"expected_version": view["version"], "cycle_number": view["cycle_number"]},
    ).json()
    apps, runtime = applications(configured, _approval(ApprovalScope.MODIFY))
    worker = Worker(configured, apps)
    claimed = worker.service.queue.claim()
    assert claimed["id"] == handoff["job_id"]
    worker._execute(claimed)  # 修复方案已保存，但队列完成状态尚未写入。
    restarted = Worker(configured, apps)
    restarted.reconcile_finished_commands()
    restarted.service.queue.recover_interrupted()
    assert restarted.service.queue.job(handoff["job_id"], "alice")["status"] == "succeeded"
    assert len(runtime.turns) == 1
    assert client.get(f"/v1/tasks/{handoff['task_id']}", headers=headers()).json()[
        "state"
    ] == "waiting_modify_approval"
