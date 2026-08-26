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

    def __init__(self, root: Path, knowledge_root: Path | None = None) -> None:
        self.data_dir = root.resolve()
        self.root = self.data_dir / "workspaces"
        self.knowledge_root = knowledge_root.resolve() if knowledge_root else None

    def prepare(self, workspace: str | Path, project: str | None = None) -> Path:
        directory = self._workspace_dir(workspace)
        self._migrate_legacy(directory)
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
        session: AgentSession,
        decision: AgentDecision,
        model: str,
    ) -> CapabilityReceipt:
        directory = self.prepare(session.workspace, session.project)
        draft = decision.capability or self._fallback_draft(session, decision)
        # One stable UUID-derived file belongs to one conversation and cannot leak a model title.
        document = directory / "capabilities" / f"{session.id}.md"
        task_record = directory / "tasks" / f"{session.id}.json"
        index_file = directory / "CAPABILITIES.md"

        workspace = str(Path(session.workspace).resolve())
        safe = lambda value: sanitize_text(value, workspace)  # noqa: E731 - renderer helper
        relative_document = document.relative_to(directory).as_posix()
        cycle = self._cycle_record(session, decision, model, safe)

        if task_record.exists():
            stored = json.loads(task_record.read_text(encoding="utf-8"))
            cycles = merge_cycle_entries(cycle_entries_from_record(stored))
            recorded = {int(item["cycle_number"]) for item in cycles}
            appendices: list[str] = []

            # v0.5.3 briefly wrote one file per cycle. Preserve those files, but fold their
            # content and metadata into the stable session document when it is next touched.
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
                appendices.append(
                    self._render_cycle_appendix(session, decision, draft, safe)
                )

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
                    else "# Recovered development capability\n"
                )
                header = capability_frontmatter(
                    workflow="development",
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
                        "changed_files": cycle["changed_files"],
                        "test_summary": cycle["test_summary"],
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

        markdown = self._render_markdown(session, decision, draft, model, safe)
        self._atomic_text(document, markdown)
        record: dict[str, Any] = {
            "schema_version": 2,
            "task_id": session.id,
            "session_id": session.id,
            "cycle_number": session.cycle_number,
            "cycle_count": 1,
            "last_cycle_number": session.cycle_number,
            "workspace_id": directory.parent.name,
            "project": session.project,
            "goal": safe(session.goal),
            "cycle_objective": safe(session.cycle_objective or session.goal),
            "outcome": safe(decision.message),
            "changed_files": [safe(path) for path in decision.changed_files],
            "test_summary": safe(decision.test_summary or ""),
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
        session: AgentSession,
        decision: AgentDecision,
        model: str,
        safe: Any,
    ) -> dict[str, Any]:
        return {
            "cycle_number": session.cycle_number,
            "cycle_objective": safe(session.cycle_objective or session.goal),
            "outcome": safe(decision.message),
            "changed_files": [safe(path) for path in decision.changed_files],
            "test_summary": safe(decision.test_summary or ""),
            "model": model,
            "completed_at": session.updated_at.isoformat(),
        }

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
        return capability_frontmatter(
            workflow="development",
            session_id=session.id,
            cycle_count=1,
            last_cycle_number=session.cycle_number,
            model=model,
            created_at=session.updated_at.isoformat(),
            updated_at=session.updated_at.isoformat(),
        ) + f"""# {safe(draft.title).replace(chr(10), " ")}

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

- 会话原始目标：{safe(session.goal)}
- 本轮目标：{safe(session.cycle_objective or session.goal)}
- 工作轮次：{session.cycle_number}
- 结果：{safe(decision.message)}
- 变更文件：{", ".join(safe(path) for path in decision.changed_files) or "无"}
"""

    @staticmethod
    def _render_cycle_appendix(
        session: AgentSession,
        decision: AgentDecision,
        draft: CapabilityDraft,
        safe: Any,
    ) -> str:
        def bullets(values: list[str], empty: str) -> str:
            return "\n".join(f"- {safe(item)}" for item in values) or f"- {empty}"

        evidence = [
            f"{item.path}: {item.summary}" if item.path else item.summary
            for item in decision.evidence
        ]
        return f"""---

## 后续工作轮次 {session.cycle_number}：{safe(draft.title).replace(chr(10), " ")}

- 本轮目标：{safe(session.cycle_objective or session.goal)}
- 完成时间：{session.updated_at.isoformat()}
- 本轮结果：{safe(decision.message)}
- 变更文件：{", ".join(safe(path) for path in decision.changed_files) or "无"}

### 总结

{safe(draft.summary)}

### 适用场景

{bullets(draft.triggers, "仅在与本轮上下文相符时参考。")}

### 方法

{bullets(draft.method, "暂无额外步骤。")}

### 验证

{bullets(draft.validation, safe(decision.test_summary or "本轮未记录可执行验证。"))}

### 风险与边界

{bullets(draft.risks, "使用前以当前代码和用户要求重新核实。")}

### 任务证据

{bullets(evidence, "本轮未记录文件证据。")}"""

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
            title = _markdown_title(document.read_text(encoding="utf-8"))
            summary = str(record.get("outcome", "")).replace("\n", " ").strip()
            cycle_count = int(record.get("cycle_count", 1))
            entries.append(
                f"- [共 {cycle_count} 轮 · {title}]({record['document']}) — {summary[:240]}"
            )
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


def capability_frontmatter(
    *,
    workflow: str,
    session_id: str,
    cycle_count: int,
    last_cycle_number: int,
    model: str,
    created_at: str,
    updated_at: str,
) -> str:
    """Render the mutable summary header for one append-only conversation document."""

    return f"""---
schema_version: 2
workflow: {json.dumps(workflow)}
session_id: {json.dumps(session_id)}
cycle_count: {cycle_count}
last_cycle_number: {last_cycle_number}
model: {json.dumps(model)}
created_at: {json.dumps(created_at)}
updated_at: {json.dumps(updated_at)}
---

"""


def markdown_body(content: str) -> str:
    """Remove existing YAML frontmatter while leaving the knowledge body unchanged."""

    if not content.startswith("---"):
        return content.lstrip()
    match = re.match(r"\A---\r?\n.*?\r?\n---\r?\n", content, flags=re.DOTALL)
    return content[match.end() :].lstrip() if match else content.lstrip()


def cycle_entries_from_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Read v2 aggregate cycles or convert a v1 per-cycle task record."""

    entries = record.get("cycles")
    if isinstance(entries, list):
        return [dict(item) for item in entries if isinstance(item, dict)]
    return [
        {
            "cycle_number": int(record.get("cycle_number", 1)),
            "cycle_objective": record.get("cycle_objective")
            or record.get("goal")
            or record.get("problem")
            or "",
            "outcome": record.get("outcome", ""),
            "changed_files": record.get("changed_files", []),
            "test_summary": record.get("test_summary", ""),
            "model": record.get("model", ""),
            "completed_at": record.get("completed_at", ""),
        }
    ]


def merge_cycle_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate cycle metadata by cycle number and return it in execution order."""

    merged: dict[int, dict[str, Any]] = {}
    for entry in entries:
        number = int(entry.get("cycle_number", 1))
        merged[number] = {**entry, "cycle_number": number}
    return [merged[number] for number in sorted(merged)]


def legacy_cycle_records(directory: Path, session_id: str) -> list[dict[str, Any]]:
    """Load v0.5.3 per-cycle records without deleting their immutable source files."""

    records: list[dict[str, Any]] = []
    for path in sorted((directory / "tasks").glob(f"{session_id}-cycle-*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def render_legacy_cycle_appendix(
    directory: Path,
    record: dict[str, Any],
    cycle_number: int,
) -> str:
    """Turn a preserved v0.5.3 cycle document into a section of the session document."""

    document = capability_document_path(directory, record)
    if document is None:
        return f"""---

## 后续工作轮次 {cycle_number}（历史记录）

- 本轮目标：{record.get("cycle_objective", "未记录")}
- 本轮结果：{record.get("outcome", "未记录")}
- 原能力文档缺失，仅保留了任务元数据。"""
    body = markdown_body(document.read_text(encoding="utf-8"))
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines.pop(0)
    nested = "\n".join(
        f"#{line}" if re.match(r"^#{2,5}\s", line) else line for line in lines
    ).strip()
    return f"""---

## 后续工作轮次 {cycle_number}（历史记录迁移）

{nested or f'- 本轮结果：{record.get("outcome", "未记录")}'}"""


def capability_document_path(
    directory: Path,
    record: dict[str, Any],
) -> Path | None:
    """Resolve a task record document without allowing it to escape its workspace memory."""

    relative = str(record.get("document", ""))
    if not relative:
        return None
    document = (directory / relative).resolve()
    return document if document.is_relative_to(directory.resolve()) and document.is_file() else None


def session_index_records(directory: Path) -> list[dict[str, Any]]:
    """Return one index record per Session, including preserved v0.5.3 cycle files."""

    grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path in (directory / "tasks").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session_id = str(record.get("session_id") or record.get("task_id") or path.stem)
        grouped.setdefault(session_id, []).append((path, record))

    result: list[dict[str, Any]] = []
    for session_id, candidates in grouped.items():
        base = next(
            (record for path, record in candidates if path.stem == session_id),
            max(
                (record for _, record in candidates),
                key=lambda item: int(item.get("cycle_number", 1)),
            ),
        )
        cycles = merge_cycle_entries(
            [
                cycle
                for _, record in candidates
                for cycle in cycle_entries_from_record(record)
            ]
        )
        latest = cycles[-1] if cycles else {}
        indexed = dict(base)
        indexed["cycle_count"] = len(cycles) or 1
        indexed["last_cycle_number"] = int(latest.get("cycle_number", 1))
        indexed["outcome"] = latest.get("outcome", indexed.get("outcome", ""))
        indexed["completed_at"] = latest.get(
            "completed_at", indexed.get("completed_at", "")
        )
        result.append(indexed)
    return result


def _markdown_title(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled capability"


def pinned_markdown_entries(directory: Path, project: str | None = None) -> list[str]:
    """Return stable links for user-maintained workspace guidance."""

    entries: list[str] = []
    pinned_root = directory / "pinned"
    if project:
        selected = (pinned_root / project / f"{project}.md").resolve()
        documents = [selected] if selected.is_relative_to(pinned_root.resolve()) else []
    else:
        documents = sorted(pinned_root.rglob("*.md"))
    for document in documents:
        if not document.is_file():
            continue
        title = _markdown_title(document.read_text(encoding="utf-8"))
        relative = document.relative_to(directory).as_posix()
        entries.append(f"- [{title}]({relative}) — 用户维护的当前工作区基础知识。")
    return entries


def sync_knowledge_documents(
    source_root: Path,
    pinned_root: Path,
    project: str | None = None,
) -> None:
    """Copy project-level knowledge into a workspace's read-only memory view."""

    if not source_root.is_dir():
        return
    if project:
        selected = (source_root / project / f"{project}.md").resolve()
        if not selected.is_relative_to(source_root.resolve()) or not selected.is_file():
            raise ValueError(f"Selected knowledge project does not exist: {project}")
        sources = [selected]
    else:
        sources = list(source_root.rglob("*.md"))
    for source in sources:
        target = pinned_root / source.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding="utf-8")
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            continue
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)


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
