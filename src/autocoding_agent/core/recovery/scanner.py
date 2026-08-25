"""Workflow-neutral orphaned Runtime scanning."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from autocoding_agent.core.recovery.models import RecoveryScanResult
from autocoding_agent.core.runtime.models import RunStatus, RuntimeRunRecord
from autocoding_agent.core.state_machine.models import TaskState


class RecoverableSession(Protocol):
    id: str
    task_state: TaskState
    runs: list[RuntimeRunRecord]


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
            for run in active:
                if not self._is_orphaned(
                    run,
                    current_owner_id=current_owner_id,
                    lease_seconds=lease_seconds,
                    now=observed_at,
                ):
                    live.append(run.id)
                    continue
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
        if run.owner_pid is not None:
            return not _pid_is_alive(run.owner_pid)
        return age_seconds >= lease_seconds


def _pid_is_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
