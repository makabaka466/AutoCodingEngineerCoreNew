"""Small, inspectable JSON session store with atomic replacement."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from autocoding_agent.core.models import AgentSession


class JsonSessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve() / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, session: AgentSession) -> None:
        path = self._path(session.id)
        if path.exists():
            raise FileExistsError(f"Session already exists: {session.id}")
        self._write(path, session)

    def load(self, session_id: str) -> AgentSession:
        path = self._path(session_id)
        if not path.exists():
            raise KeyError(f"Unknown session: {session_id}")
        return AgentSession.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, session: AgentSession) -> None:
        self._write(self._path(session.id), session)

    def list(self) -> list[AgentSession]:
        sessions = [
            AgentSession.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.root.glob("*.json")
        ]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def _path(self, session_id: str) -> Path:
        # UUID parsing prevents a caller from turning a session id into a path.
        safe_id = str(UUID(session_id))
        return self.root / f"{safe_id}.json"

    @staticmethod
    def _write(path: Path, session: AgentSession) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(session.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
