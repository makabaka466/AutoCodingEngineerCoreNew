"""Workspace-scoped capability memory written after truthful task completion."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from autocoding_agent.core.models import AgentDecision, AgentSession, CapabilityDraft


class CapabilityReceipt(BaseModel):
    document_path: str
    index_path: str
    created: bool


class CapabilityStore:
    """Persist human-readable learning without touching the user's repository."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve() / "workspaces"

    def prepare(self, workspace: str | Path) -> Path:
        directory = self._workspace_dir(workspace)
        self._migrate_legacy(directory)
        (directory / "capabilities").mkdir(parents=True, exist_ok=True)
        (directory / "pinned").mkdir(parents=True, exist_ok=True)
        (directory / "tasks").mkdir(parents=True, exist_ok=True)
        self._rebuild_index(directory)
        return directory

    def record(
        self,
        session: AgentSession,
        decision: AgentDecision,
        model: str,
    ) -> CapabilityReceipt:
        directory = self.prepare(session.workspace)
        draft = decision.capability or self._fallback_draft(session, decision)
        # A UUID-only filename cannot leak a secret that the model placed in a title.
        document = directory / "capabilities" / f"{session.id}.md"
        task_record = directory / "tasks" / f"{session.id}.json"
        index_file = directory / "CAPABILITIES.md"

        if task_record.exists():
            stored = json.loads(task_record.read_text(encoding="utf-8"))
            self._rebuild_index(directory)
            return CapabilityReceipt(
                document_path=str(directory / stored["document"]),
                index_path=str(index_file),
                created=False,
            )

        workspace = str(Path(session.workspace).resolve())
        safe = lambda value: sanitize_text(value, workspace)  # noqa: E731 - renderer helper
        markdown = self._render_markdown(session, decision, draft, model, safe)
        self._atomic_text(document, markdown)

        relative_document = document.relative_to(directory).as_posix()
        record: dict[str, Any] = {
            "schema_version": 1,
            "task_id": session.id,
            "session_id": session.id,
            "workspace_id": directory.parent.name,
            "goal": safe(session.goal),
            "outcome": safe(decision.message),
            "changed_files": [safe(path) for path in decision.changed_files],
            "test_summary": safe(decision.test_summary or ""),
            "document": relative_document,
            "model": model,
            "completed_at": session.updated_at.isoformat(),
        }
        self._atomic_text(task_record, json.dumps(record, ensure_ascii=False, indent=2))
        self._rebuild_index(directory)
        return CapabilityReceipt(
            document_path=str(document),
            index_path=str(index_file),
            created=True,
        )

    def _workspace_dir(self, workspace: str | Path) -> Path:
        canonical = str(Path(workspace).resolve()).casefold()
        workspace_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return self.root / workspace_id / "development"

    @staticmethod
    def _migrate_legacy(directory: Path) -> None:
        """Copy pre-domain capability files once, preserving the original layout."""

        if directory.exists():
            return
        legacy = directory.parent
        if not (legacy / "CAPABILITIES.md").is_file():
            return
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("capabilities", "tasks"):
            source = legacy / name
            if source.is_dir():
                shutil.copytree(source, directory / name, dirs_exist_ok=True)
        shutil.copy2(legacy / "CAPABILITIES.md", directory / "CAPABILITIES.md")

    @staticmethod
    def _fallback_draft(session: AgentSession, decision: AgentDecision) -> CapabilityDraft:
        return CapabilityDraft(
            title=f"Task learning {session.id[:8]}",
            summary=decision.message,
            triggers=[session.goal],
            method=decision.next_actions
            or ["Review the recorded evidence before reusing this result."],
            validation=[decision.test_summary] if decision.test_summary else [],
            risks=[
                "This fallback was derived from the final result because no explicit "
                "draft was returned."
            ],
        )

    @staticmethod
    def _render_markdown(
        session: AgentSession,
        decision: AgentDecision,
        draft: CapabilityDraft,
        model: str,
        safe: Any,
    ) -> str:
        def bullets(values: list[str], empty: str) -> str:
            return "\n".join(f"- {safe(item)}" for item in values) or f"- {empty}"

        evidence = [
            f"{item.path}: {item.summary}" if item.path else item.summary
            for item in decision.evidence
        ]
        return f"""---
schema_version: 1
session_id: {json.dumps(session.id)}
model: {json.dumps(model)}
completed_at: {json.dumps(session.updated_at.isoformat())}
---

# {safe(draft.title).replace(chr(10), " ")}

{safe(draft.summary)}

## 适用场景

{bullets(draft.triggers, "仅在与本次任务上下文相符时参考。")}

## 方法

{bullets(draft.method, "暂无额外步骤。")}

## 验证

{bullets(draft.validation, safe(decision.test_summary or "本次未记录可执行验证。"))}

## 风险与边界

{bullets(draft.risks, "使用前以当前代码和用户要求重新核实。")}

## 任务证据

{bullets(evidence, "本次未记录文件证据。")}

## 来源任务

- 目标：{safe(session.goal)}
- 结果：{safe(decision.message)}
- 变更文件：{", ".join(safe(path) for path in decision.changed_files) or "无"}
"""

    def _rebuild_index(self, directory: Path) -> None:
        pinned = pinned_markdown_entries(directory)
        entries: list[str] = []
        for record_file in sorted((directory / "tasks").glob("*.json")):
            record = json.loads(record_file.read_text(encoding="utf-8"))
            document = directory / record["document"]
            title = _markdown_title(document.read_text(encoding="utf-8"))
            summary = str(record.get("outcome", "")).replace("\n", " ").strip()
            entries.append(f"- [{title}]({record['document']}) — {summary[:240]}")
        content = (
            """# Development Capabilities

This index contains historical, model-distilled guidance for this workspace. It may be stale. Read
only entries relevant to the current task and verify them against current repository evidence.

## 固定工作区知识

"""
            + ("\n".join(pinned) if pinned else "No pinned workspace guidance yet.")
            + """

## 已完成开发任务

"""
            + ("\n".join(entries) if entries else "No completed-task capabilities yet.")
            + "\n"
        )
        self._atomic_text(directory / "CAPABILITIES.md", content)

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


def _markdown_title(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled capability"


def pinned_markdown_entries(directory: Path) -> list[str]:
    """Return stable links for user-maintained workspace guidance."""

    entries: list[str] = []
    for document in sorted((directory / "pinned").glob("*.md")):
        title = _markdown_title(document.read_text(encoding="utf-8"))
        relative = document.relative_to(directory).as_posix()
        entries.append(f"- [{title}]({relative}) — 用户维护的当前工作区基础知识。")
    return entries


def sanitize_text(value: str, workspace: str) -> str:
    text = re.sub(re.escape(workspace), "<WORKSPACE>", str(value), flags=re.IGNORECASE)
    text = re.sub(re.escape(str(Path.home())), "<USER_HOME>", text, flags=re.IGNORECASE)
    # Bearer must be removed before the generic key/value rule sees only its prefix.
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|auth(?:orization)?|token|password|secret)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", text)
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return text.strip()
