"""Atomic JSON persistence for incident sessions."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from autocoding_agent.incident.models import IncidentSession


class JsonIncidentStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve() / "incidents"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, session: IncidentSession) -> None:
        path = self._path(session.id)
        if path.exists():
            raise FileExistsError(f"Incident session already exists: {session.id}")
        self._write(path, session)

    def load(self, session_id: str) -> IncidentSession:
        path = self._path(session_id)
        if not path.exists():
            raise KeyError(f"Unknown incident session: {session_id}")
        return IncidentSession.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, session: IncidentSession) -> None:
        self._write(self._path(session.id), session)

    def list(self) -> list[IncidentSession]:
        sessions = [
            IncidentSession.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.root.glob("*.json")
        ]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{UUID(session_id)}.json"

    @staticmethod
    def _write(path: Path, session: IncidentSession) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(session.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
