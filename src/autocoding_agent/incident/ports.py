"""Infrastructure ports for incident sessions and read-only data access."""

from typing import Protocol

from autocoding_agent.incident.models import IncidentSession


class IncidentSessionStore(Protocol):
    def create(self, session: IncidentSession) -> None: ...

    def load(self, session_id: str) -> IncidentSession: ...

    def save(self, session: IncidentSession) -> None: ...

    def list(self) -> list[IncidentSession]: ...
