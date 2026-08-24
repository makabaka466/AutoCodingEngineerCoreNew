"""Editable, workspace-scoped Markdown knowledge organized by workflow and branch."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from autocoding_agent.adapters.capability_store import CapabilityStore
from autocoding_agent.incident.capability_store import IncidentCapabilityStore


class KnowledgeDomain(StrEnum):
    DEVELOPMENT = "development"
    INCIDENT = "incident"


class WorkspaceKnowledgeError(ValueError):
    """A safe validation error suitable for display in the settings UI."""


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    name: str
    path: Path


class MarkdownKnowledgeService:
    """Manage pinned Markdown without exposing another workflow's files."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.workspaces_root = self.data_dir / "workspaces"

    def list_branches(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
    ) -> list[str]:
        root = self._pinned_root(workspace, domain)
        if not root.is_dir():
            return []
        return sorted(
            (item.name for item in root.iterdir() if item.is_dir()),
            key=str.casefold,
        )

    def create_branch(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        name: str,
    ) -> Path:
        path = self.branch_path(workspace, domain, name)
        if path.exists() and not path.is_dir():
            raise WorkspaceKnowledgeError(f"同名文件已经存在：{path.name}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def branch_path(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
    ) -> Path:
        return self._branch_path(workspace, domain, branch)

    def list_documents(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
    ) -> list[MarkdownDocument]:
        branch_path = self._branch_path(workspace, domain, branch)
        if not branch_path.is_dir():
            return []
        return [
            MarkdownDocument(name=item.name, path=item)
            for item in sorted(branch_path.glob("*.md"), key=lambda item: item.name.casefold())
        ]

    def create_document(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
        filename: str,
    ) -> Path:
        path = self._document_path(workspace, domain, branch, filename)
        if path.exists():
            raise WorkspaceKnowledgeError(f"Markdown 文件已经存在：{path.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        title = path.stem.replace("-", " ").replace("_", " ").strip() or "新建知识"
        self._atomic_text(path, f"# {title}\n\n")
        self.refresh_index(workspace, domain)
        return path

    def load_document(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
        filename: str,
    ) -> tuple[Path, str]:
        path = self._document_path(workspace, domain, branch, filename)
        if not path.is_file():
            raise WorkspaceKnowledgeError(f"Markdown 文件不存在：{path.name}")
        return path, path.read_text(encoding="utf-8")

    def save_document(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
        filename: str,
        content: str,
    ) -> Path:
        path = self._document_path(workspace, domain, branch, filename)
        if not path.is_file():
            raise WorkspaceKnowledgeError("请先选择或新建一个 Markdown 文件。")
        if not content.strip():
            raise WorkspaceKnowledgeError("Markdown 内容不能为空。")
        self._atomic_text(path, content.rstrip() + "\n")
        self.refresh_index(workspace, domain)
        return path

    def migrate_root_documents(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
    ) -> list[Path]:
        """Move legacy pinned/*.md files into one explicit secondary branch."""

        root = self._pinned_root(workspace, domain)
        branch_path = self.create_branch(workspace, domain, branch)
        moved: list[Path] = []
        for source in sorted(root.glob("*.md")):
            target = branch_path / source.name
            if target.exists():
                raise WorkspaceKnowledgeError(f"迁移目标已经存在：{target}")
            source.replace(target)
            moved.append(target)
        self.refresh_index(workspace, domain)
        return moved

    def refresh_index(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
    ) -> Path:
        if domain == KnowledgeDomain.DEVELOPMENT:
            return CapabilityStore(self.data_dir).prepare(workspace)
        return IncidentCapabilityStore(self.data_dir).prepare(workspace)

    def _pinned_root(self, workspace: str | Path, domain: KnowledgeDomain) -> Path:
        canonical = Path(workspace).expanduser().resolve(strict=True)
        if not canonical.is_dir():
            raise WorkspaceKnowledgeError(f"项目路径不是目录：{canonical}")
        workspace_id = hashlib.sha256(
            str(canonical).casefold().encode("utf-8")
        ).hexdigest()[:16]
        return self.workspaces_root / workspace_id / domain.value / "pinned"

    def _branch_path(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
    ) -> Path:
        root = self._pinned_root(workspace, domain)
        candidate = (root / _safe_segment(branch, "二级分支")).resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise WorkspaceKnowledgeError("二级分支路径超出能力目录。")
        return candidate

    def _document_path(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
        filename: str,
    ) -> Path:
        cleaned = _safe_segment(filename, "Markdown 文件名")
        if not cleaned.casefold().endswith(".md"):
            cleaned += ".md"
        return self._branch_path(workspace, domain, branch) / cleaned

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


_INVALID_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *{f"com{number}" for number in range(1, 10)},
    *{f"lpt{number}" for number in range(1, 10)},
}


def _safe_segment(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise WorkspaceKnowledgeError(f"{label}不能为空。")
    if len(cleaned) > 80:
        raise WorkspaceKnowledgeError(f"{label}不能超过 80 个字符。")
    if cleaned in {".", ".."} or cleaned.endswith((".", " ")):
        raise WorkspaceKnowledgeError(f"{label}不是有效的 Windows 名称。")
    if _INVALID_SEGMENT.search(cleaned):
        raise WorkspaceKnowledgeError(f"{label}不能包含 Windows 路径保留字符。")
    if cleaned.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        raise WorkspaceKnowledgeError(f"{label}使用了 Windows 保留名称。")
    return cleaned
