"""Local content and metadata boundary for immutable task artifacts."""

from typing import Any, Protocol

from autocoding_agent.core.artifacts.models import ArtifactRecord, ArtifactType


class ArtifactStore(Protocol):
    def write_text(
        self,
        *,
        task_id: str,
        event_id: str,
        artifact_type: ArtifactType,
        content: str,
        source: str,
        host_verified: bool,
        related_paths: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        suffix: str | None = None,
    ) -> ArtifactRecord: ...

    def resolve_path(self, artifact: ArtifactRecord) -> str: ...
