"""Create durable decision rationale inside the task aggregate."""

from __future__ import annotations

from autocoding_agent.core.audit.models import (
    ChangeExplanation,
    DecisionRecord,
    EvidenceRef,
    RiskLevel,
)
from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentSession,
    AgentStatus,
    ApprovalScope,
    EventType,
)


class DecisionRecorder:
    def record(
        self,
        session: AgentSession,
        decision: AgentDecision,
        *,
        model: str,
        runtime_session_id: str | None,
        command_id: str | None,
        actor: str = "model",
    ) -> DecisionRecord:
        event = AgentEvent(
            type=EventType.DECISION_RECORDED,
            message=f"Recorded {decision.status.value} decision rationale.",
            actor=actor,
            command_id=command_id,
            data={"decision_type": decision.status.value},
        )
        evidence = [EvidenceRef(path=item.path, summary=item.summary) for item in decision.evidence]
        evidence_paths = {item.path for item in evidence if item.path}
        proposal_paths = (
            [item.path for item in decision.approval.proposal.changes if item.path]
            if decision.approval is not None and decision.approval.proposal is not None
            else []
        )
        evidence.extend(
            EvidenceRef(
                path=path,
                summary=(
                    "The model proposed or reported this file; verify execution against "
                    "host-observed artifacts."
                ),
            )
            for path in dict.fromkeys([*proposal_paths, *decision.changed_files])
            if path not in evidence_paths
        )
        record = DecisionRecord(
            task_id=session.id,
            event_id=event.id,
            decision_type=decision.status.value,
            summary=decision.message,
            reason=decision.reason or decision.message,
            evidence=evidence,
            alternatives=decision.alternatives,
            confidence=decision.confidence,
            risk_level=decision.risk_level or self._default_risk(decision),
            actor=actor,
            model=model,
            runtime_session_id=runtime_session_id,
        )
        session.decision_records.append(record)
        session.events.append(event)
        return record

    @staticmethod
    def explain_change(session: AgentSession, path: str) -> ChangeExplanation:
        normalized = path.replace("\\", "/").casefold()
        decisions = [
            record
            for record in session.decision_records
            if any(
                item.path and item.path.replace("\\", "/").casefold() == normalized
                for item in record.evidence
            )
        ]
        artifacts = [
            artifact
            for artifact in session.artifacts
            if any(
                item.replace("\\", "/").casefold() == normalized for item in artifact.related_paths
            )
        ]
        if decisions:
            summary = (
                f"Found {len(decisions)} decision record(s) and {len(artifacts)} "
                "related artifact(s)."
            )
        else:
            summary = "No recorded decision directly references this workspace-relative path."
        return ChangeExplanation(
            task_id=session.id,
            path=path,
            decisions=decisions,
            artifact_ids=[artifact.id for artifact in artifacts],
            artifacts=artifacts,
            event_ids=list(
                dict.fromkeys(
                    [record.event_id for record in decisions]
                    + [artifact.event_id for artifact in artifacts]
                )
            ),
            summary=summary,
        )

    @staticmethod
    def _default_risk(decision: AgentDecision) -> RiskLevel:
        if decision.status == AgentStatus.FAILED:
            return RiskLevel.HIGH
        if decision.approval is not None and decision.approval.scope == ApprovalScope.MODIFY:
            return RiskLevel.HIGH
        if decision.status in {AgentStatus.APPROVAL_REQUIRED, AgentStatus.QUERY_REQUIRED}:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW
