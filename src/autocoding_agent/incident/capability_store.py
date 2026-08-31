"""Incident-specific capability documents kept separate from development learning."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autocoding_agent.adapters.capability_store import (
    CapabilityReceipt,
    capability_document_path,
    capability_frontmatter,
    cycle_entries_from_record,
    legacy_cycle_records,
    markdown_body,
    merge_cycle_entries,
    pinned_markdown_entries,
    render_legacy_cycle_appendix,
    sanitize_text,
    session_index_records,
    sync_knowledge_documents,
)
from autocoding_agent.incident.models import (
    IncidentDecision,
    IncidentSession,
    QueryObservation,
    QueryObservationStatus,
)


def _query_observation_summary(item: QueryObservation) -> str:
    stage = item.stage or "unspecified"
    if item.status == QueryObservationStatus.FAILED:
        return f"[{stage}] {item.query_name}: failed ({item.error or 'unknown error'})"
    summary = f"[{stage}] {item.query_name}: {item.returned_rows} rows"
    if item.truncated:
        summary += ", truncated"
    if item.redacted_columns:
        summary += f", redacted columns: {', '.join(item.redacted_columns)}"
    return summary


class IncidentCapabilityStore:
    """Maintain one sanitized diagnostic capability document per incident conversation."""

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
        document = directory / "capabilities" / f"{session.id}.md"
        task_record = directory / "tasks" / f"{session.id}.json"
        index_file = directory / "CAPABILITIES.md"

        workspace = str(Path(session.workspace).resolve())
        safe = lambda value: sanitize_text(value, workspace)  # noqa: E731
        relative_document = document.relative_to(directory).as_posix()
        cycle = self._cycle_record(session, decision, model, safe)

        if task_record.exists():
            stored = json.loads(task_record.read_text(encoding="utf-8"))
            cycles = merge_cycle_entries(cycle_entries_from_record(stored))
            recorded = {int(item["cycle_number"]) for item in cycles}
            appendices: list[str] = []

            for legacy in legacy_cycle_records(directory, session.id):
                for legacy_cycle in cycle_entries_from_record(legacy):
                    number = int(legacy_cycle["cycle_number"])
                    if number in recorded:
                        continue
                    cycles.append(legacy_cycle)
                    recorded.add(number)
                    appendices.append(render_legacy_cycle_appendix(directory, legacy, number))

            if session.cycle_number not in recorded:
                cycles.append(cycle)
                recorded.add(session.cycle_number)
                appendices.append(self._render_cycle_appendix(session, decision, safe))

            cycles = merge_cycle_entries(cycles)
            if appendices:
                created_at = str(
                    stored.get("created_at")
                    or stored.get("completed_at")
                    or cycles[0].get("completed_at")
                    or session.updated_at.isoformat()
                )
                body = markdown_body(
                    document.read_text(encoding="utf-8")
                    if document.is_file()
                    else "# Recovered incident capability\n"
                )
                header = capability_frontmatter(
                    workflow="incident",
                    session_id=session.id,
                    cycle_count=len(cycles),
                    last_cycle_number=max(recorded),
                    model=model,
                    created_at=created_at,
                    updated_at=session.updated_at.isoformat(),
                )
                markdown = header + body.rstrip() + "\n\n" + "\n\n".join(appendices) + "\n"
                self._atomic_text(document, markdown)
                stored.update(
                    {
                        "schema_version": 2,
                        "cycle_number": session.cycle_number,
                        "cycle_count": len(cycles),
                        "last_cycle_number": max(recorded),
                        "cycle_objective": cycle["cycle_objective"],
                        "outcome": cycle["outcome"],
                        "model": model,
                        "completed_at": session.updated_at.isoformat(),
                        "updated_at": session.updated_at.isoformat(),
                        "created_at": created_at,
                        "document": relative_document,
                        "cycles": cycles,
                    }
                )
                self._atomic_text(task_record, json.dumps(stored, ensure_ascii=False, indent=2))

            self._rebuild_index(directory, session.project)
            return CapabilityReceipt(
                document_path=str(document),
                index_path=str(index_file),
                created=False,
            )

        self._atomic_text(document, self._render(session, decision, model, safe))
        record: dict[str, Any] = {
            "schema_version": 2,
            "domain": "incident",
            "task_id": session.id,
            "session_id": session.id,
            "cycle_number": session.cycle_number,
            "cycle_count": 1,
            "last_cycle_number": session.cycle_number,
            "workspace_id": directory.parent.name,
            "project": session.project,
            "problem": safe(session.problem),
            "cycle_objective": safe(session.cycle_objective or session.problem),
            "outcome": safe(decision.message),
            "document": relative_document,
            "model": model,
            "completed_at": session.updated_at.isoformat(),
            "created_at": session.updated_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "cycles": [cycle],
        }
        self._atomic_text(task_record, json.dumps(record, ensure_ascii=False, indent=2))
        self._rebuild_index(directory, session.project)
        return CapabilityReceipt(
            document_path=str(document),
            index_path=str(index_file),
            created=True,
        )

    @staticmethod
    def _cycle_record(
        session: IncidentSession,
        decision: IncidentDecision,
        model: str,
        safe: Any,
    ) -> dict[str, Any]:
        return {
            "cycle_number": session.cycle_number,
            "cycle_objective": safe(session.cycle_objective or session.problem),
            "outcome": safe(decision.message),
            "diagnosis": safe(decision.diagnosis or decision.message),
            "recommended_actions": [safe(item) for item in decision.recommended_actions],
            "model": model,
            "completed_at": session.updated_at.isoformat(),
        }

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
            _query_observation_summary(item)
            for item in session.query_observations[session.cycle_query_observation_start :]
        ]

        def bullets(values: list[str], empty: str) -> str:
            return "\n".join(f"- {safe(item)}" for item in values) or f"- {empty}"

        return capability_frontmatter(
            workflow="incident",
            session_id=session.id,
            cycle_count=1,
            last_cycle_number=session.cycle_number,
            model=model,
            created_at=session.updated_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
        ) + f"""# 异常能力：{page_name}

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

    @staticmethod
    def _render_cycle_appendix(
        session: IncidentSession,
        decision: IncidentDecision,
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
            _query_observation_summary(item)
            for item in session.query_observations[session.cycle_query_observation_start :]
        ]

        def bullets(values: list[str], empty: str) -> str:
            return "\n".join(f"- {safe(item)}" for item in values) or f"- {empty}"

        return f"""---

## 后续诊断轮次 {session.cycle_number}

- 本轮问题：{safe(session.cycle_objective or session.problem)}
- 完成时间：{session.updated_at.isoformat()}
- 页面：{page_name}
- 路由：{route}

### 代码定位

{bullets(paths, "本轮未记录代码路径。")}

### 诊断结论

{safe(decision.diagnosis or decision.message)}

### 证据与发现

{bullets(findings, "本轮未记录额外发现。")}

### 数据核查审计

{bullets(observations, "本轮未执行数据库查询。")}

### 建议动作

{bullets(decision.recommended_actions, "本轮未记录后续动作。")}

### 自动化边界

- 自动化候选：{"是" if decision.automation_candidate else "否"}
- 置信度：{decision.confidence if decision.confidence is not None else "未记录"}
- 复用前必须以当前代码、当前数据权限和最新业务状态重新验证。"""

    def _rebuild_index(self, directory: Path, project: str | None = None) -> None:
        pinned = pinned_markdown_entries(directory, project)
        entries: list[str] = []
        records = session_index_records(directory)
        records.sort(
            key=lambda item: (
                str(item.get("completed_at", "")),
                str(item.get("session_id", "")),
                int(item.get("last_cycle_number", item.get("cycle_number", 1))),
            )
        )
        for record in records:
            document = capability_document_path(directory, record)
            if document is None:
                continue
            title = next(
                (
                    line[2:].strip()
                    for line in document.read_text(encoding="utf-8").splitlines()
                    if line.startswith("# ")
                ),
                "Untitled incident capability",
            )
            summary = str(record.get("outcome", "")).replace("\n", " ").strip()
            cycle_count = int(record.get("cycle_count", 1))
            entries.append(
                f"- [共 {cycle_count} 轮 · {title}]({record['document']}) — {summary[:240]}"
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
