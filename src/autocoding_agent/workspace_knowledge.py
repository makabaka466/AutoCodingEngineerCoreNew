"""Project-local Markdown knowledge organized by workflow and secondary path."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class KnowledgeDomain(StrEnum):
    DEVELOPMENT = "development"
    INCIDENT = "incident"


class WorkspaceKnowledgeError(ValueError):
    """A safe validation error suitable for display in the settings UI."""


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


class MarkdownKnowledgeService:
    """Manage one project-local Markdown document per workflow secondary path."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        self.project_root = Path(project_root or PROJECT_ROOT).expanduser().resolve()
        self.knowledge_root = self.project_root / "knowledge"

    def list_branches(self, domain: KnowledgeDomain) -> list[str]:
        root = self._domain_root(domain)
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

    def create_branch(self, domain: KnowledgeDomain, name: str) -> Path:
        path = self.branch_path(domain, name)
        if path.parent.exists():
            raise WorkspaceKnowledgeError(f"二级路径已经存在：{path.stem}")
        path.parent.mkdir(parents=True)
        self._atomic_text(path, f"# {path.stem}\n\n")
        return path

    def branch_path(self, domain: KnowledgeDomain, branch: str) -> Path:
        root = self._domain_root(domain)
        cleaned = _safe_segment(branch, "二级路径")
        directory = (root / cleaned).resolve()
        if not directory.is_relative_to(root.resolve()):
            raise WorkspaceKnowledgeError("二级路径超出能力目录。")
        return directory / f"{cleaned}.md"

    def relative_path(self, path: str | Path) -> str:
        """Return a stable project-relative path for display and documentation."""

        return Path(path).resolve().relative_to(self.project_root).as_posix()

    def load_branch(
        self,
        domain: KnowledgeDomain,
        branch: str,
    ) -> tuple[Path, str]:
        path = self.branch_path(domain, branch)
        if not path.is_file():
            raise WorkspaceKnowledgeError(f"二级路径不存在：{path.stem}")
        return path, path.read_text(encoding="utf-8")

    def save_branch(
        self,
        domain: KnowledgeDomain,
        branch: str,
        content: str,
    ) -> Path:
        path = self.branch_path(domain, branch)
        if not path.is_file():
            raise WorkspaceKnowledgeError("请先选择或添加一个二级路径。")
        if not content.strip():
            raise WorkspaceKnowledgeError("Markdown 内容不能为空。")
        self._atomic_text(path, content.rstrip() + "\n")
        return path

    def _domain_root(self, domain: KnowledgeDomain) -> Path:
        return self.knowledge_root / domain.value

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
