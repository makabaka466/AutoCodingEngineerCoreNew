"""Workflow-neutral orphaned Runtime scanning."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from autocoding_agent.core.models import AgentEvent, EventType
from autocoding_agent.core.recovery.models import RecoveryScanResult
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.models import TaskState


class RecoverableSession(Protocol):
    id: str
    task_state: TaskState
    runs: list[RuntimeRunRecord]
    events: list[AgentEvent]


class RecoverySessionStore(Protocol):
    def list(self) -> list[RecoverableSession]: ...


class OrphanedRunScanner:
    """Find abandoned leases and delegate workflow-specific state updates."""

    def __init__(
        self,
        sessions: RecoverySessionStore,
        *,
        is_terminal: Callable[[TaskState], bool],
        recover: Callable[[RecoverableSession, RuntimeRunRecord, datetime], None],
    ) -> None:
        self.sessions = sessions
        self.is_terminal = is_terminal
        self.recover = recover

    def reconcile(
        self,
        *,
        current_owner_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> RecoveryScanResult:
        observed_at = now or datetime.now(timezone.utc)
        recovered: list[str] = []
        live: list[str] = []
        for session in self.sessions.list():
            if self.is_terminal(session.task_state):
                continue
            active = [run for run in session.runs if run.status == RunStatus.STARTED]
            # 覆盖上下文准备、宿主查询以及 Run 完成后尚未保存流程结果的窗口。
            preparing = None
            if not active and session.task_state in {
                TaskState.CREATED, TaskState.INSPECTING, TaskState.IMPLEMENTING,
                TaskState.VERIFYING, TaskState.QUERYING_DATA, TaskState.REPLANNING,
            }:
                turn = next((event for event in reversed(session.events)
                             if event.type in {EventType.TURN_STARTED, EventType.TASK_CREATED}),
                            None)
                last_run = session.runs[-1] if session.runs else None
                if turn and (last_run is None or (
                    turn.data.get("owner_id") and turn.created_at > last_run.started_at
                )):
                    preparing = RuntimeRunRecord(
                        task_id=session.id, state=session.task_state,
                        mode=turn.data.get("mode", "inspect"),
                        owner_id=turn.data.get("owner_id"),
                        owner_pid=turn.data.get("owner_pid"),
                        started_at=turn.created_at, heartbeat_at=turn.created_at,
                    )
                    active = [preparing]
                elif last_run is not None:
                    active = [last_run]
            for run in active:
                if not self._is_orphaned(
                    run,
                    current_owner_id=current_owner_id,
                    lease_seconds=lease_seconds,
                    now=observed_at,
                ):
                    live.append(run.id)
                    continue
                if run is preparing:
                    session.runs.append(run)
                self.recover(session, run, observed_at)
                recovered.append(session.id)
                break
        return RecoveryScanResult(
            recovered_task_ids=list(dict.fromkeys(recovered)),
            skipped_live_run_ids=live,
        )

    @staticmethod
    def _is_orphaned(
        run: RuntimeRunRecord,
        *,
        current_owner_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> bool:
        if run.owner_id == current_owner_id:
            return False
        age_seconds = max(0.0, (now - run.heartbeat_at).total_seconds())
        if run.runtime_pid is not None and _pid_is_alive(run.runtime_pid):
            return False
        if run.owner_pid is not None:
            return not _pid_is_alive(run.owner_pid)
        return age_seconds >= lease_seconds


def _pid_is_alive(pid: int) -> bool:
    """只读检查进程；未知权限状态按存活处理，避免误接管活跃任务。"""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _windows_pid_is_alive(pid: int) -> bool:
    # Windows 的 os.kill(pid, 0) 实际调用 TerminateProcess，不能用于探活。
    # 仅申请 SYNCHRONIZE 权限，零等待观察句柄；从不申请终止或写权限。
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER：PID 不存在
    try:
        # WAIT_OBJECT_0 表示进程已退出；超时或查询失败都不能作为接管依据。
        return kernel32.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel32.CloseHandle(handle)
