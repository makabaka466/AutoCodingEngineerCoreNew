"""Create task artifacts from model decisions and host-observed workspace state."""

from __future__ import annotations

import json
from typing import Protocol

from autocoding_agent.core.artifacts.models import (
    ArtifactRecord,
    ArtifactType,
    WorkspaceSnapshot,
)
from autocoding_agent.core.audit.models import DecisionRecord
from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentMode,
    AgentSession,
    ApprovalScope,
    EventType,
)
from autocoding_agent.core.runtime.models import RuntimeRunRecord
from autocoding_agent.ports.artifact_store import ArtifactStore


class WorkspaceObserver(Protocol):
    def capture(self, workspace: str) -> WorkspaceSnapshot: ...


class ArtifactRecorder:
    """Turn bounded facts into immutable content plus append-only metadata events."""

    def __init__(self, store: ArtifactStore, workspace_observer: WorkspaceObserver) -> None:
        self.store = store
        self.workspace_observer = workspace_observer

    def record_baseline(self, session: AgentSession, command_id: str) -> list[ArtifactRecord]:
        snapshot = self.workspace_observer.capture(session.workspace)
        common = {
            "dirty": snapshot.dirty,
            "is_git": snapshot.is_git,
            "git_commit": snapshot.git_commit,
            "truncated": snapshot.truncated,
            "observation_error": snapshot.error,
            "snapshot_role": "pre_implement",
        }
        status = self._write(
            session,
            command_id=command_id,
            artifact_type=ArtifactType.BASELINE_STATUS,
            content=json.dumps(snapshot.status_payload(), ensure_ascii=False, indent=2),
            source="host_git_observation",
            host_verified=True,
            related_paths=list(snapshot.related_paths),
            metadata=common,
        )
        patch = self._write(
            session,
            command_id=command_id,
            artifact_type=ArtifactType.BASELINE_PATCH,
            content=self._patch_content(snapshot),
            source="host_git_observation",
            host_verified=True,
            related_paths=list(snapshot.related_paths),
            metadata={**common, "baseline_status_id": status.id},
        )
        return [status, patch]

    def record_post_implementation(
        self,
        session: AgentSession,
        command_id: str,
    ) -> list[ArtifactRecord]:
        snapshot = self.workspace_observer.capture(session.workspace)
        baseline_status = self._latest(session, ArtifactType.BASELINE_STATUS)
        baseline_patch = self._latest(session, ArtifactType.BASELINE_PATCH)
        baseline_dirty = bool(baseline_status and baseline_status.metadata.get("dirty", False))
        attribution = (
            "The workspace baseline was dirty. Compare the baseline and current artifacts "
            "before attributing individual edits to this Agent turn."
            if baseline_dirty
            else "The baseline was clean. The current tracked diff is attributable to this "
            "authorized turn unless another process edited the workspace concurrently."
        )
        common = {
            "dirty": snapshot.dirty,
            "is_git": snapshot.is_git,
            "git_commit": snapshot.git_commit,
            "truncated": snapshot.truncated,
            "observation_error": snapshot.error,
            "snapshot_role": "post_implement",
            "baseline_status_id": baseline_status.id if baseline_status else None,
            "baseline_patch_id": baseline_patch.id if baseline_patch else None,
            "baseline_was_dirty": baseline_dirty,
            "attribution": attribution,
        }
        context = self._write(
            session,
            command_id=command_id,
            artifact_type=ArtifactType.CONTEXT,
            content=json.dumps(snapshot.status_payload(), ensure_ascii=False, indent=2),
            source="host_git_observation",
            host_verified=True,
            related_paths=list(snapshot.related_paths),
            metadata=common,
        )
        patch = self._write(
            session,
            command_id=command_id,
            artifact_type=ArtifactType.CHANGES_PATCH,
            content=self._patch_content(snapshot),
            source="host_git_observation",
            host_verified=True,
            related_paths=list(snapshot.related_paths),
            metadata={**common, "post_status_id": context.id},
        )
        return [context, patch]

    def record_decision_artifacts(
        self,
        session: AgentSession,
        decision: AgentDecision,
        decision_record: DecisionRecord,
        mode: AgentMode,
        command_id: str,
    ) -> list[ArtifactRecord]:
        records: list[ArtifactRecord] = []
        related_paths = self._decision_paths(decision)
        if mode == AgentMode.INSPECT:
            records.append(
                self._write(
                    session,
                    command_id=command_id,
                    artifact_type=ArtifactType.ANALYSIS,
                    content=json.dumps(
                        {
                            "schema_version": 1,
                            "decision_id": decision_record.id,
                            "status": decision.status.value,
                            "summary": decision.message,
                            "reason": decision.reason,
                            "alternatives": decision.alternatives,
                            "confidence": decision.confidence,
                            "risk_level": (
                                decision.risk_level.value if decision.risk_level else None
                            ),
                            "evidence": [
                                item.model_dump(mode="json") for item in decision.evidence
                            ],
                            "next_actions": decision.next_actions,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    source="model_decision",
                    host_verified=False,
                    related_paths=related_paths,
                    metadata={"decision_id": decision_record.id},
                )
            )
        if (
            decision.approval is not None
            and decision.approval.scope == ApprovalScope.MODIFY
            and decision.approval.proposal is not None
        ):
            records.append(
                self._write(
                    session,
                    command_id=command_id,
                    artifact_type=ArtifactType.PROPOSAL,
                    content=json.dumps(
                        {
                            "schema_version": 1,
                            "decision_id": decision_record.id,
                            "proposal": decision.approval.proposal.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    source="model_proposal",
                    host_verified=False,
                    related_paths=related_paths,
                    metadata={"decision_id": decision_record.id},
                )
            )
        if decision.test_summary:
            records.append(
                self._write(
                    session,
                    command_id=command_id,
                    artifact_type=ArtifactType.TEST_RESULT,
                    content=json.dumps(
                        {
                            "schema_version": 1,
                            "decision_id": decision_record.id,
                            "reported_by": "model",
                            "summary": decision.test_summary,
                            "host_verified": False,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    source="model_report",
                    host_verified=False,
                    related_paths=related_paths,
                    metadata={
                        "decision_id": decision_record.id,
                        "verification_limit": (
                            "Runtime tool lifecycle evidence is not available yet."
                        ),
                    },
                )
            )
        return records

    def record_final_report(
        self,
        session: AgentSession,
        decision: AgentDecision,
        decision_record: DecisionRecord,
        command_id: str,
    ) -> ArtifactRecord:
        changed = "\n".join(f"- `{path}`" for path in decision.changed_files) or "- 无"
        tests = decision.test_summary or "未记录可由宿主确认的验证结果。"
        content = f"""# Task Final Report

- Task ID: `{session.id}`
- Work cycle: `{session.cycle_number}`
- Decision ID: `{decision_record.id}`
- Project: `{session.project or "未选择"}`

## Goal

{session.cycle_objective or session.goal}

## Outcome

{decision.message}

## Model-reported changed files

{changed}

## Validation

{tests}

> 本报告由宿主根据模型结构化结果生成。真实修改范围请以 host-verified baseline、context 和
> changes patch Artifact 为准；测试事实需等待 Runtime 生命周期事件进一步核验。
"""
        return self._write(
            session,
            command_id=command_id,
            artifact_type=ArtifactType.FINAL_REPORT,
            content=content,
            source="host_rendered_model_summary",
            host_verified=False,
            related_paths=self._decision_paths(decision),
            metadata={"decision_id": decision_record.id},
        )

    def record_recovery_report(
        self,
        session: AgentSession,
        run: RuntimeRunRecord,
        command_id: str,
    ) -> ArtifactRecord:
        snapshot = self.workspace_observer.capture(session.workspace)
        baseline_status = self._latest(session, ArtifactType.BASELINE_STATUS)
        baseline_patch = self._latest(session, ArtifactType.BASELINE_PATCH)
        content = json.dumps(
            {
                "schema_version": 1,
                "task_id": session.id,
                "run_id": run.id,
                "interrupted_state": run.state.value,
                "interrupted_mode": run.mode,
                "last_heartbeat": run.heartbeat_at.isoformat(),
                "baseline_status_id": baseline_status.id if baseline_status else None,
                "baseline_patch_id": baseline_patch.id if baseline_patch else None,
                "current_workspace": snapshot.status_payload(),
                "current_tracked_patch": self._patch_content(snapshot),
                "recovery_policy": (
                    "No write or verification action may be replayed automatically. "
                    "Choose a read-only inspection, replan, or cancel."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        return self._write(
            session,
            command_id=command_id,
            artifact_type=ArtifactType.RECOVERY_REPORT,
            content=content,
            source="host_recovery_observation",
            host_verified=True,
            related_paths=list(snapshot.related_paths),
            metadata={
                "run_id": run.id,
                "interrupted_mode": run.mode,
                "baseline_status_id": baseline_status.id if baseline_status else None,
                "baseline_patch_id": baseline_patch.id if baseline_patch else None,
                "workspace_dirty": snapshot.dirty,
            },
        )

    def _write(
        self,
        session: AgentSession,
        *,
        command_id: str,
        artifact_type: ArtifactType,
        content: str,
        source: str,
        host_verified: bool,
        related_paths: list[str],
        metadata: dict[str, object],
    ) -> ArtifactRecord:
        event = AgentEvent(
            type=EventType.ARTIFACT_RECORDED,
            message=f"Recorded {artifact_type.value} artifact.",
            actor="host",
            command_id=command_id,
            data={
                "artifact_type": artifact_type.value,
                "cycle_number": session.cycle_number,
            },
        )
        record = self.store.write_text(
            task_id=session.id,
            event_id=event.id,
            artifact_type=artifact_type,
            content=content,
            source=source,
            host_verified=host_verified,
            related_paths=related_paths,
            metadata={**metadata, "cycle_number": session.cycle_number},
        )
        event.data.update(
            {
                "artifact_id": record.id,
                "relative_path": record.relative_path,
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "host_verified": record.host_verified,
                "redacted": record.sensitive,
            }
        )
        session.artifacts.append(record)
        session.events.append(event)
        return record

    @staticmethod
    def _patch_content(snapshot: WorkspaceSnapshot) -> str:
        if snapshot.patch:
            return snapshot.patch
        if not snapshot.is_git:
            return "# No Git patch available: workspace is not a Git worktree.\n"
        if snapshot.related_paths:
            return (
                "# No tracked Git patch content. The status artifact lists untracked or "
                "metadata-only changes.\n"
            )
        return "# Clean Git worktree: no staged or unstaged tracked diff.\n"

    @staticmethod
    def _decision_paths(decision: AgentDecision) -> list[str]:
        paths = [item.path for item in decision.evidence if item.path]
        if decision.approval is not None and decision.approval.proposal is not None:
            paths.extend(item.path for item in decision.approval.proposal.changes if item.path)
        paths.extend(decision.changed_files)
        return list(dict.fromkeys(paths))

    @staticmethod
    def _latest(
        session: AgentSession,
        artifact_type: ArtifactType,
    ) -> ArtifactRecord | None:
        return next(
            (
                artifact
                for artifact in reversed(session.artifacts)
                if artifact.type == artifact_type
            ),
            None,
        )
