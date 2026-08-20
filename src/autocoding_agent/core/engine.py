"""The small stateful kernel that coordinates model turns and hard boundaries."""

from __future__ import annotations

from pathlib import Path

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.core.models import (
    AgentDecision,
    AgentEvent,
    AgentMode,
    AgentOutcome,
    AgentSession,
    AgentStatus,
    ApprovalScope,
    ChatMessage,
    EventType,
    MessageRole,
    RuntimeTurn,
    utc_now,
)
from autocoding_agent.core.policies import ExecutionPolicy
from autocoding_agent.ports.runtime import AgentRuntime
from autocoding_agent.ports.session_store import SessionStore
from autocoding_agent.skills import SkillRegistry


class PolicyViolation(RuntimeError):
    pass


class AgentEngine:
    """One task per session, with true multi-turn clarification and approval resume."""

    def __init__(
        self,
        runtime: AgentRuntime,
        sessions: SessionStore,
        capabilities: CapabilityStore,
        skills: SkillRegistry,
        policy: ExecutionPolicy,
        model: str,
    ) -> None:
        self.runtime = runtime
        self.sessions = sessions
        self.capabilities = capabilities
        self.skills = skills
        self.policy = policy
        self.model = model

    def start(self, workspace: str | Path, message: str) -> AgentOutcome:
        canonical = Path(workspace).expanduser().resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError(f"Workspace is not a directory: {canonical}")
        if not message.strip():
            raise ValueError("Task message cannot be empty.")
        session = AgentSession(workspace=str(canonical), goal=message.strip())
        self.sessions.create(session)
        return self._execute(session, message.strip(), AgentMode.INSPECT)

    def send(self, session_id: str, message: str) -> AgentOutcome:
        session = self.sessions.load(session_id)
        if session.status == AgentStatus.COMPLETED:
            raise ValueError("This task is complete. Start a new session for a new task.")
        if not message.strip():
            raise ValueError("Message cannot be empty.")
        # A normal reply while approval is pending is treated as a revised instruction.
        session.pending_approval = None
        return self._execute(session, message.strip(), AgentMode.INSPECT)

    def approve(self, session_id: str) -> AgentOutcome:
        session = self.sessions.load(session_id)
        approval = session.pending_approval
        if approval is None:
            raise ValueError("This session has no pending approval request.")
        session.pending_approval = None
        mode = AgentMode.IMPLEMENT if approval.scope == ApprovalScope.MODIFY else AgentMode.VERIFY
        message = (
            f"The user approved the requested {approval.scope.value} scope for this task. "
            "Continue from the existing investigation within that exact scope."
        )
        return self._execute(session, message, mode)

    def reject(self, session_id: str, reason: str = "") -> AgentOutcome:
        session = self.sessions.load(session_id)
        approval = session.pending_approval
        if approval is None:
            raise ValueError("This session has no pending approval request.")
        session.pending_approval = None
        detail = f" Reason: {reason.strip()}" if reason.strip() else ""
        message = (
            f"The user declined the requested {approval.scope.value} scope.{detail} "
            "Continue without that permission and provide the best truthful alternative."
        )
        return self._execute(session, message, AgentMode.INSPECT)

    def get_session(self, session_id: str) -> AgentSession:
        return self.sessions.load(session_id)

    def list_sessions(self) -> list[AgentSession]:
        return self.sessions.list()

    def outcome(self, session_id: str) -> AgentOutcome:
        return self._to_outcome(self.sessions.load(session_id))

    def _execute(
        self,
        session: AgentSession,
        user_message: str,
        mode: AgentMode,
    ) -> AgentOutcome:
        session.messages.append(ChatMessage(role=MessageRole.USER, content=user_message))
        session.events.append(
            AgentEvent(
                type=EventType.TURN_STARTED,
                message=f"Started {mode.value} turn.",
                data={"mode": mode.value},
            )
        )
        session.updated_at = utc_now()
        self.sessions.save(session)

        capability_dir = self.capabilities.prepare(session.workspace)
        profile = self.policy.profile(mode)
        # The memory directory is outside the target workspace. Mount it only when no
        # write/command tool exists; resumed modes retain anything already read.
        readable_capability_dir = str(capability_dir) if mode == AgentMode.INSPECT else None
        existing_runtime_session_id = session.runtime_session_id
        if existing_runtime_session_id is None:
            # Claude receives the application UUID on its first turn. Persist that fact
            # before launch so a timeout cannot cause the same side-effecting turn to be
            # replayed later as another "new" session.
            session.runtime_session_id = session.id
            self.sessions.save(session)
        turn = RuntimeTurn(
            session_id=session.id,
            runtime_session_id=existing_runtime_session_id,
            workspace=session.workspace,
            user_message=user_message,
            history=session.messages[:-1],
            mode=mode,
            system_prompt=self.skills.build_system_prompt(mode, readable_capability_dir),
            tools=list(profile.tools),
            allowed_tools=list(profile.allowed_tools),
            permission_mode=profile.permission_mode,
            capability_dir=readable_capability_dir,
        )

        try:
            result = self.runtime.run(turn)
            self._validate_decision(result.decision, mode)
        except Exception as exc:  # Runtime and policy failures become a durable task state.
            decision = AgentDecision(status=AgentStatus.FAILED, message=str(exc))
            session.last_decision = decision
            session.status = decision.status
            session.pending_approval = None
            session.messages.append(
                ChatMessage(role=MessageRole.ASSISTANT, content=decision.message)
            )
            session.events.append(AgentEvent(type=EventType.TASK_FAILED, message=decision.message))
            session.updated_at = utc_now()
            self.sessions.save(session)
            return self._to_outcome(session)

        decision = result.decision
        session.runtime_session_id = result.runtime_session_id
        session.last_decision = decision
        session.last_usage = result.usage
        session.status = decision.status
        session.pending_approval = decision.approval
        session.messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=decision.message))
        session.events.append(
            AgentEvent(
                type=EventType.RUNTIME_FINISHED,
                message="Claude Code returned a validated decision.",
                data={"status": decision.status.value},
            )
        )
        self._append_status_event(session, decision)
        session.updated_at = utc_now()

        if decision.status == AgentStatus.COMPLETED:
            try:
                receipt = self.capabilities.record(session, decision, self.model)
                session.capability_document = receipt.document_path
                session.events.append(
                    AgentEvent(
                        type=EventType.CAPABILITY_SAVED,
                        message="Saved reusable capability knowledge.",
                        data={"path": receipt.document_path, "created": receipt.created},
                    )
                )
            except Exception as exc:
                # Memory is secondary: a successful software task remains successful.
                session.events.append(
                    AgentEvent(
                        type=EventType.CAPABILITY_FAILED,
                        message=f"Task completed, but capability storage failed: {exc}",
                    )
                )

        session.updated_at = utc_now()
        self.sessions.save(session)
        return self._to_outcome(session)

    @staticmethod
    def _validate_decision(decision: AgentDecision, mode: AgentMode) -> None:
        if mode == AgentMode.INSPECT and decision.changed_files:
            raise PolicyViolation(
                "The model reported file changes during a read-only inspect turn."
            )
        for candidate in [*decision.changed_files, *[item.path for item in decision.evidence]]:
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_absolute() or ".." in path.parts:
                raise PolicyViolation(f"Model returned an out-of-workspace path: {candidate}")

    @staticmethod
    def _append_status_event(session: AgentSession, decision: AgentDecision) -> None:
        event_type = {
            AgentStatus.NEEDS_INPUT: EventType.INPUT_REQUIRED,
            AgentStatus.APPROVAL_REQUIRED: EventType.APPROVAL_REQUIRED,
            AgentStatus.COMPLETED: EventType.TASK_COMPLETED,
            AgentStatus.FAILED: EventType.TASK_FAILED,
        }[decision.status]
        session.events.append(
            AgentEvent(type=event_type, message=decision.message, data={"status": decision.status})
        )

    @staticmethod
    def _to_outcome(session: AgentSession) -> AgentOutcome:
        decision = session.last_decision
        if decision is None or session.status is None:
            raise ValueError(f"Session {session.id} has not produced an outcome yet.")
        return AgentOutcome(
            session_id=session.id,
            workspace=session.workspace,
            status=session.status,
            message=decision.message,
            evidence=decision.evidence,
            next_actions=decision.next_actions,
            approval=session.pending_approval,
            changed_files=decision.changed_files,
            test_summary=decision.test_summary,
            capability_document=session.capability_document,
            usage=session.last_usage,
            events=session.events,
        )
