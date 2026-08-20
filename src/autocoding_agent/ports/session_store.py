"""Session persistence port."""

from typing import Protocol

from autocoding_agent.core.models import AgentSession


class SessionStore(Protocol):
    def create(self, session: AgentSession) -> None: ...

    def load(self, session_id: str) -> AgentSession: ...

    def save(self, session: AgentSession) -> None: ...

    def list(self) -> list[AgentSession]: ...
