"""Editable, workspace-scoped Markdown knowledge organized by workflow and branch."""

from __future__ import annotations

import hashlib
import re
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


class MarkdownKnowledgeService:
    """Manage one Markdown document per secondary workflow branch."""

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
            (
                item.name
                for item in root.iterdir()
                if item.is_dir() and (item / f"{item.name}.md").is_file()
            ),
            key=str.casefold,
        )

    def create_branch(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        name: str,
    ) -> Path:
        path = self.branch_path(workspace, domain, name)
        if path.parent.exists():
            raise WorkspaceKnowledgeError(f"二级分支已经存在：{path.stem}")
        path.parent.mkdir(parents=True)
        self._atomic_text(path, f"# {path.stem}\n\n")
        self.refresh_index(workspace, domain)
        return path

    def branch_path(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
    ) -> Path:
        root = self._pinned_root(workspace, domain)
        cleaned = _safe_segment(branch, "二级分支")
        directory = (root / cleaned).resolve()
        if not directory.is_relative_to(root.resolve()):
            raise WorkspaceKnowledgeError("二级路径超出能力目录。")
        return directory / f"{cleaned}.md"

    def load_branch(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
    ) -> tuple[Path, str]:
        path = self.branch_path(workspace, domain, branch)
        if not path.is_file():
            raise WorkspaceKnowledgeError(f"二级分支不存在：{path.stem}")
        return path, path.read_text(encoding="utf-8")

    def save_branch(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
        content: str,
    ) -> Path:
        path = self.branch_path(workspace, domain, branch)
        if not path.is_file():
            raise WorkspaceKnowledgeError("请先选择或添加一个二级分支。")
        if not content.strip():
            raise WorkspaceKnowledgeError("Markdown 内容不能为空。")
        self._atomic_text(path, content.rstrip() + "\n")
        self.refresh_index(workspace, domain)
        return path

    def migrate_flat_branch(
        self,
        workspace: str | Path,
        domain: KnowledgeDomain,
        branch: str,
    ) -> Path:
        """Move legacy pinned/<branch>.md into pinned/<branch>/<branch>.md."""

        target = self.branch_path(workspace, domain, branch)
        if target.exists():
            return target
        source = self._pinned_root(workspace, domain) / (
            f"{_safe_segment(branch, '二级分支')}.md"
        )
        if not source.is_file():
            raise WorkspaceKnowledgeError(f"旧分支文档不存在：{source}")
        target.parent.mkdir(parents=True)
        source.replace(target)
        self.refresh_index(workspace, domain)
        return target

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
