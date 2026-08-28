"""Immutable, redacted task-artifact files stored outside target workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from autocoding_agent.core.artifacts.models import ArtifactRecord, ArtifactType


class ArtifactTooLarge(ValueError):
    """The caller attempted to persist an unexpectedly large text artifact."""


class ArtifactIntegrityError(RuntimeError):
    """An immutable artifact or its manifest no longer matches the write request."""


_DEFAULT_SUFFIX = {
    ArtifactType.ANALYSIS: ".json",
    ArtifactType.CONTEXT: ".json",
    ArtifactType.PROPOSAL: ".json",
    ArtifactType.BASELINE_STATUS: ".json",
    ArtifactType.BASELINE_PATCH: ".patch",
    ArtifactType.CHANGES_PATCH: ".patch",
    ArtifactType.TEST_RESULT: ".json",
    ArtifactType.RECOVERY_REPORT: ".json",
    ArtifactType.HERMES_SKILL_RESULT: ".json",
    ArtifactType.FINAL_REPORT: ".md",
}


class TaskArtifactStore:
    """Write content atomically and maintain a local immutable manifest per task."""

    def __init__(self, root: str | Path, *, max_bytes: int = 2 * 1024 * 1024) -> None:
        if max_bytes < 1024:
            raise ValueError("max_bytes must be at least 1024")
        self.data_dir = Path(root).expanduser().resolve()
        self.root = self.data_dir / "tasks"
        self.max_bytes = max_bytes

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
    ) -> ArtifactRecord:
        safe_task_id = str(UUID(task_id))
        artifact_id = str(uuid4())
        sanitized, redacted = redact_artifact_text(content)
        payload = sanitized.encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ArtifactTooLarge(
                f"Artifact is {len(payload)} bytes; maximum is {self.max_bytes}."
            )

        artifact_dir = self.root / safe_task_id / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        extension = suffix or _DEFAULT_SUFFIX[artifact_type]
        if not extension.startswith(".") or not extension[1:].isalnum():
            raise ValueError("Artifact suffix must be a simple extension.")
        # Keep filenames UUID-only. Besides preventing model text from leaking into names,
        # this leaves enough headroom for Windows installations without long-path support.
        filename = f"{artifact_id}{extension}"
        target = artifact_dir / filename
        relative_path = target.relative_to(self.data_dir).as_posix()
        record = ArtifactRecord(
            id=artifact_id,
            task_id=safe_task_id,
            event_id=event_id,
            type=artifact_type,
            relative_path=relative_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            source=source,
            host_verified=host_verified,
            sensitive=redacted,
            related_paths=list(dict.fromkeys(related_paths or [])),
            metadata={**(metadata or {}), "redacted": redacted},
        )

        self._atomic_create(target, payload)
        try:
            self._append_manifest(artifact_dir / "manifest.json", record)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return record

    def resolve_path(self, artifact: ArtifactRecord) -> str:
        candidate = (self.data_dir / artifact.relative_path).resolve()
        expected_root = (self.root / str(UUID(artifact.task_id)) / "artifacts").resolve()
        if not candidate.is_relative_to(expected_root):
            raise ArtifactIntegrityError("Artifact path escapes its task directory.")
        return str(candidate)

    def verify(self, artifact: ArtifactRecord) -> bool:
        path = Path(self.resolve_path(artifact))
        if not path.is_file():
            return False
        payload = path.read_bytes()
        return (
            len(payload) == artifact.size_bytes
            and hashlib.sha256(payload).hexdigest() == artifact.sha256
        )

    def _append_manifest(self, manifest: Path, record: ArtifactRecord) -> None:
        if manifest.is_file():
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("artifacts"), list):
                raise ArtifactIntegrityError("Artifact manifest has an invalid shape.")
        else:
            raw = {"schema_version": 1, "task_id": record.task_id, "artifacts": []}
        if raw.get("task_id") != record.task_id:
            raise ArtifactIntegrityError("Artifact manifest belongs to another task.")
        if any(item.get("id") == record.id for item in raw["artifacts"]):
            raise ArtifactIntegrityError(f"Artifact {record.id} already exists in manifest.")
        raw["artifacts"].append(record.model_dump(mode="json"))
        self._atomic_replace(
            manifest,
            json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    @staticmethod
    def _atomic_create(target: Path, payload: bytes) -> None:
        if target.exists():
            raise FileExistsError(f"Artifact already exists: {target.name}")
        temporary = target.with_name(f".tmp-{uuid4().hex[:12]}")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_replace(target: Path, payload: bytes) -> None:
        temporary = target.with_name(f".tmp-{uuid4().hex[:12]}")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def redact_artifact_text(value: str) -> tuple[str, bool]:
    """Remove common credentials before local evidence is written to disk."""

    original = str(value)
    text = re.sub(
        r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        original,
    )
    text = re.sub(
        r"(?i)\b(api[_-]?key|auth(?:orization)?|token|password|passwd|secret|pwd)"
        r"\s*[:=]\s*[^\s;,]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", text)
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return text, text != original
