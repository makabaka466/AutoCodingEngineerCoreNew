from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autocoding_agent.adapters.sqlite_incident_store import SQLiteIncidentStore
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentEvent, AgentUsage, EventType, RuntimeTurn
from autocoding_agent.core.recovery.models import RecoveryAction
from autocoding_agent.core.recovery.scanner import _pid_is_alive
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.machine import AgentStateMachine
from autocoding_agent.core.state_machine.models import TaskState
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.incident.models import (
    IncidentDecision,
    IncidentSession,
    IncidentStatus,
    LocatedPage,
)
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class CompleteAfterResumeRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[IncidentDecision],
    ) -> StructuredRuntimeResult[IncidentDecision]:
        self.calls += 1
        return StructuredRuntimeResult(
            output=IncidentDecision(
                status=IncidentStatus.COMPLETED,
                message="Recovered diagnosis complete.",
                page=LocatedPage(
                    name="Orders",
                    source_paths=["src/orders.py"],
                    matched_evidence=["The page was rechecked after recovery."],
                    explanation="The page was rechecked after recovery.",
                ),
                diagnosis="The task resumed from current read-only evidence.",
            ),
            runtime_session_id=turn.session_id,
            usage=AgentUsage(turns=1),
        )


def test_orphaned_incident_run_pauses_and_resumes_without_automatic_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    session = IncidentSession(workspace=str(workspace), problem="Order page is stale")
    session.events.append(
        AgentEvent(type=EventType.TASK_CREATED, message="Created incident.", actor="user")
    )
    machine = AgentStateMachine()
    machine.transition(session, TaskState.INSPECTING, reason="Started incident inspection.")
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    session.runs.append(
        RuntimeRunRecord(
            task_id=session.id,
            state=TaskState.INSPECTING,
            mode="inspect",
            owner_id="dead-owner",
            owner_pid=99_999_999,
            started_at=old,
            heartbeat_at=old,
            runtime_session_id="incident-before-crash",
        )
    )
    SQLiteIncidentStore(data_dir, migrate_legacy_json=False).create(session)
    runtime = CompleteAfterResumeRuntime()
    settings = Settings(
        claude_command="claude-test.exe",
        claude_model="test-model",
        claude_timeout_seconds=30,
        data_dir=data_dir,
        runtime_lease_seconds=5,
    )

    application = build_incident_application(settings=settings, runtime=runtime)
    paused = application.get_session(session.id)

    assert application.recovery_scan.recovered_task_ids == [session.id]
    assert paused.task_state == TaskState.PAUSED
    assert paused.runs[0].status == RunStatus.INTERRUPTED
    assert runtime.calls == 0

    completed = application.resume(session.id, RecoveryAction.READ_ONLY_INSPECT)

    assert completed.task_state == TaskState.COMPLETED
    assert runtime.calls == 1
    assert SQLiteIncidentStore(data_dir).replay_task_state(session.id) == TaskState.COMPLETED


def test_real_owner_process_is_not_killed_and_can_recover_after_interruption(tmp_path: Path):
    """真实宿主进程持久化 Run 后中断，验证重启扫描和显式恢复；不调用外部服务。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, hermes_skills_enabled=False)
    store = SQLiteIncidentStore(data_dir)
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(workspace), str(data_dir)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + 15
        sessions = []
        while time.monotonic() < deadline:
            sessions = store.list()
            if sessions and sessions[0].runs:
                break
            if child.poll() is not None:
                raise AssertionError(child.communicate()[1].decode(errors="replace"))
            time.sleep(0.05)
        assert sessions and sessions[0].runs, "Worker did not persist a Runtime run"
        session_id = sessions[0].id
        run_id = sessions[0].runs[0].id
        assert sessions[0].runs[0].owner_pid == child.pid

        # 第二个宿主启动也不能终止或接管仍活跃的第一个宿主。
        runtime = CompleteAfterResumeRuntime()
        other = build_incident_application(settings=settings, runtime=runtime)
        assert other.recovery_scan.recovered_task_ids == []
        assert other.recovery_scan.skipped_live_run_ids == [run_id]
        assert _pid_is_alive(child.pid)
        assert child.poll() is None
        assert runtime.calls == 0

        # 只终止本测试创建的确切子进程，不扫描或关闭用户的 ACE/Claude 进程。
        child.terminate()
        child.wait(timeout=5)
        assert not _pid_is_alive(child.pid)
        restarted = build_incident_application(settings=settings, runtime=runtime)
        paused = restarted.get_session(session_id)
        assert paused.task_state == TaskState.PAUSED
        assert paused.runs[0].status == RunStatus.INTERRUPTED
        assert runtime.calls == 0
        assert store.replay_task_state(session_id) == TaskState.PAUSED

        completed = restarted.resume(session_id, RecoveryAction.READ_ONLY_INSPECT)
        assert completed.task_state == TaskState.COMPLETED
        assert runtime.calls == 1
        assert store.replay_task_state(session_id) == TaskState.COMPLETED
        final = store.load(session_id)
        assert [run.status for run in final.runs] == [RunStatus.INTERRUPTED, RunStatus.COMPLETED]
        assert len([e for e in final.events if e.type == EventType.TASK_COMPLETED]) == 1
    finally:
        if child.poll() is None:
            child.terminate()
        child.communicate(timeout=5)


if __name__ == "__main__":
    class PendingRuntime:
        def run_structured(self, turn, response_model):
            # 宿主在进入 Runtime 前已提交 SQLite，等待父测试主动模拟进程中断。
            time.sleep(30)
            raise TimeoutError("Test owner was not interrupted in time")

    build_incident_application(
        settings=Settings(data_dir=Path(sys.argv[2]), hermes_skills_enabled=False),
        runtime=PendingRuntime(),
    ).start(Path(sys.argv[1]), "Investigate the test page")
