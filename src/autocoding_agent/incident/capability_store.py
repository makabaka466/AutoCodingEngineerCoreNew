"""Incident-specific capability documents kept separate from development learning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autocoding_agent.adapters.capability_store import (
    CapabilityReceipt,
    capability_cycle_key,
    pinned_markdown_entries,
    sanitize_text,
    sync_knowledge_documents,
)
from autocoding_agent.incident.models import IncidentDecision, IncidentSession


class IncidentCapabilityStore:
    """Write one sanitized diagnostic capability document per completed incident."""

    def __init__(self, root: Path, knowledge_root: Path | None = None) -> None:
        self.data_dir = root.resolve()
        self.root = self.data_dir / "workspaces"
        self.knowledge_root = knowledge_root.resolve() if knowledge_root else None

    def prepare(self, workspace: str | Path, project: str | None = None) -> Path:
        directory = self._workspace_dir(workspace)
        (directory / "capabilities").mkdir(parents=True, exist_ok=True)
        (directory / "pinned").mkdir(parents=True, exist_ok=True)
        (directory / "tasks").mkdir(parents=True, exist_ok=True)
        if self.knowledge_root is not None:
            sync_knowledge_documents(
                self.knowledge_root, directory / "pinned", project
            )
        self._rebuild_index(directory, project)
        return directory

    def record(
        self,
        session: IncidentSession,
        decision: IncidentDecision,
        model: str,
    ) -> CapabilityReceipt:
        directory = self.prepare(session.workspace, session.project)
        record_key = capability_cycle_key(session.id, session.cycle_number)
        document = directory / "capabilities" / f"{record_key}.md"
        task_record = directory / "tasks" / f"{record_key}.json"
        index_file = directory / "CAPABILITIES.md"
        if task_record.exists():
            stored = json.loads(task_record.read_text(encoding="utf-8"))
            self._rebuild_index(directory, session.project)
            return CapabilityReceipt(
                document_path=str(directory / stored["document"]),
                index_path=str(index_file),
                created=False,
            )

        workspace = str(Path(session.workspace).resolve())
        safe = lambda value: sanitize_text(value, workspace)  # noqa: E731
        self._atomic_text(document, self._render(session, decision, model, safe))
        relative_document = document.relative_to(directory).as_posix()
        record: dict[str, Any] = {
            "schema_version": 1,
            "domain": "incident",
            "task_id": session.id,
            "session_id": session.id,
            "cycle_number": session.cycle_number,
            "workspace_id": directory.parent.name,
            "project": session.project,
            "problem": safe(session.problem),
            "cycle_objective": safe(session.cycle_objective or session.problem),
            "outcome": safe(decision.message),
            "document": relative_document,
            "model": model,
            "completed_at": session.updated_at.isoformat(),
        }
        self._atomic_text(task_record, json.dumps(record, ensure_ascii=False, indent=2))
        self._rebuild_index(directory, session.project)
        return CapabilityReceipt(
            document_path=str(document),
            index_path=str(index_file),
            created=True,
        )

    def _workspace_dir(self, workspace: str | Path) -> Path:
        canonical = str(Path(workspace).resolve()).casefold()
        workspace_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return self.root / workspace_id / "incident"

    @staticmethod
    def _render(
        session: IncidentSession,
        decision: IncidentDecision,
        model: str,
        safe: Any,
    ) -> str:
        page = decision.page
        page_name = safe(page.name) if page else "未确认页面"
        route = safe(page.route) if page and page.route else "未记录"
        paths = [*(page.source_paths if page else []), *(page.related_paths if page else [])]
        findings = [
            f"{item.summary}: {'; '.join(item.evidence)}" if item.evidence else item.summary
            for item in decision.findings
        ]
        observations = [
            f"{item.query_name}: {item.returned_rows} rows"
            + (", truncated" if item.truncated else "")
            + (
                f", redacted columns: {', '.join(item.redacted_columns)}"
                if item.redacted_columns
                else ""
            )
            for item in session.query_observations[session.cycle_query_observation_start :]
        ]

        def bullets(values: list[str], empty: str) -> str:
            return "\n".join(f"- {safe(item)}" for item in values) or f"- {empty}"

        return f"""---
schema_version: 1
domain: incident
session_id: {json.dumps(session.id)}
cycle_number: {session.cycle_number}
model: {json.dumps(model)}
completed_at: {json.dumps(session.updated_at.isoformat())}
---

# 异常能力：{page_name}

## 适用问题

- 会话原始问题：{safe(session.problem)}
- 本轮问题：{safe(session.cycle_objective or session.problem)}
- 工作轮次：{session.cycle_number}
- 页面线索：{safe(session.page_hint or "未提供")}

## 页面与代码定位

- 页面：{page_name}
- 路由：{route}
{bullets(paths, "本次未记录代码路径。")}

## 诊断结论

{safe(decision.diagnosis or decision.message)}

## 证据与发现

{bullets(findings, "本次未记录额外发现。")}

## 数据核查审计

{bullets(observations, "本次未执行数据库查询。")}

## 建议动作

{bullets(decision.recommended_actions, "本次未记录后续动作。")}

## 自动化边界

- 自动化候选：{"是" if decision.automation_candidate else "否"}
- 置信度：{decision.confidence if decision.confidence is not None else "未记录"}
- 复用前必须以当前代码、当前数据权限和最新业务状态重新验证。
"""

    def _rebuild_index(self, directory: Path, project: str | None = None) -> None:
        pinned = pinned_markdown_entries(directory, project)
        entries: list[str] = []
        records = [
            json.loads(record_file.read_text(encoding="utf-8"))
            for record_file in (directory / "tasks").glob("*.json")
        ]
        records.sort(
            key=lambda item: (
                str(item.get("completed_at", "")),
                str(item.get("session_id", "")),
                int(item.get("cycle_number", 1)),
            )
        )
        for record in records:
            document = directory / record["document"]
            title = next(
                (
                    line[2:].strip()
                    for line in document.read_text(encoding="utf-8").splitlines()
                    if line.startswith("# ")
                ),
                "Untitled incident capability",
            )
            summary = str(record.get("outcome", "")).replace("\n", " ").strip()
            cycle = int(record.get("cycle_number", 1))
            entries.append(
                f"- [第 {cycle} 轮 · {title}]({record['document']}) — {summary[:240]}"
            )
        content = (
            """# Incident Capabilities

This index contains historical diagnostic guidance for this workspace. It may be stale. Read only
entries relevant to the current incident and verify them against current code and authorized data.

## 固定工作区知识

"""
            + ("\n".join(pinned) if pinned else "No pinned workspace guidance yet.")
            + """

## 已完成异常诊断

"""
            + ("\n".join(entries) if entries else "No completed-incident capabilities yet.")
            + "\n"
        )
        self._atomic_text(directory / "CAPABILITIES.md", content)

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
