"""有限文件审批核对，适用于 Git、SVN 和普通目录，不递归扫描代码库。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from autocoding_agent.core.approval_execution_models import (
    ApprovalSnapshot,
    FileFingerprint,
    ImplementationReport,
)
from autocoding_agent.core.workflow import cycle_events, read_fingerprint

if TYPE_CHECKING:
    from autocoding_agent.core.models import AgentSession, ApprovalRequest


MAX_SNAPSHOT_FILES = 128
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_path(workspace: str, reference: str) -> str | None:
    """限定为明确工作区内路径；目录或通配表达式不能充当文件授权。"""
    if not reference.strip() or any(char in reference for char in "*?\0"):
        return None
    try:
        root = Path(workspace).resolve()
        target = (root / reference.replace("\\", "/")).resolve()
        if not target.is_relative_to(root) or target == root or target.is_dir():
            return None
        return os.path.normcase(str(target.relative_to(root))).replace("\\", "/")
    except (OSError, ValueError, RuntimeError):
        return None


def _fingerprints(workspace: str, paths: Iterable[str]) -> tuple[FileFingerprint, ...]:
    result: list[FileFingerprint] = []
    remaining = MAX_SNAPSHOT_BYTES
    for path in paths:
        state, sha256 = "unverified", None
        try:
            # 再次解析，防止审阅期间路径被替换为指向工作区外的符号链接。
            normalized = normalized_path(workspace, path)
            target = Path(workspace) / path
            if normalized == path and not target.exists():
                state = "missing"
            elif normalized == path and target.is_file():
                size = target.stat().st_size
                if size <= remaining:
                    remaining -= size
                    sha256 = read_fingerprint(workspace, path)
                    if sha256:
                        state = "present"
        except (OSError, ValueError):
            pass
        result.append(FileFingerprint(path=path, state=state, sha256=sha256))
    return tuple(result)


def _context_sha256(session: AgentSession) -> str:
    # 不绑定不断变化的存储版本/运行事件；只绑定审批赖以成立的任务输入和调查上下文。
    fields = {
        "id", "workspace", "project", "goal", "cycle_number", "cycle_objective",
        "source_incident_key", "source_incident_revision", "task_analysis",
        "investigation_entry", "database_binding", "database_reference",
    }
    context = session.model_dump(mode="json", include=fields)
    context["user_messages"] = [
        item.model_dump(mode="json") for item in session.messages if item.role.value == "user"
    ]
    return _digest(_canonical(context))


def snapshot_approval(session: AgentSession, approval: ApprovalRequest) -> ApprovalSnapshot:
    """在展示待审方案时冻结内容及文件现状；有缺口时保存原因而非默许写入。"""
    planned: dict[str, None] = {}
    issues: list[str] = []
    if approval.scope.value == "modify":
        if approval.proposal is None:
            issues.append("修改方案缺少完整的改动内容，请重新生成方案。")
        for change in approval.proposal.changes if approval.proposal else ():
            path = normalized_path(session.workspace, change.path or "")
            if path is None:
                issues.append(f"改动项「{change.area}」缺少明确的工作区内文件路径。")
            else:
                planned[path] = None

    reads: dict[str, str | None] = {}
    for event in cycle_events(session):
        data = event.data
        tool = str(data.get("tool_name", "")).casefold()
        if (event.type.value == "tool_finished" and tool in {"read", "write", "edit"}
                and not data.get("is_error", True)):
            path = normalized_path(session.workspace, str(data.get("path") or ""))
            # 已成功实施的文件使用最新宿主写入指纹；不让先前正常 Read 阻断后续验证。
            if path is not None and (tool == "read" or path in reads):
                reads[path] = data.get("content_sha256")
    # 验证审批继承已批准实施的文件范围，即使旧 Runtime 不提供逐工具事件也能核对。
    # 这里只保存当前基线，绝不扩充验证阶段的写权限。
    execution = getattr(session, "approved_execution", None)
    inherited = (
        [item.path for item in execution.files]
        if approval.scope.value == "verify" and execution is not None else []
    )
    paths = list(dict.fromkeys([*planned, *reads, *inherited]))
    if len(paths) > MAX_SNAPSHOT_FILES:
        issues.append(f"审批涉及文件超过 {MAX_SNAPSHOT_FILES} 个，请拆分方案后审阅。")
    files = _fingerprints(session.workspace, paths[:MAX_SNAPSHOT_FILES])
    for item in files:
        if item.state == "unverified":
            issues.append(f"无法核对文件「{item.path}」，请缩小范围或重新读取。")
        if item.path in reads and (not reads[item.path] or reads[item.path] != item.sha256):
            issues.append(f"读取依据「{item.path}」已变化或缺少指纹，请重新调查。")
    approval_json = _canonical(approval.model_dump(mode="json"))
    return ApprovalSnapshot(
        approval_json=approval_json,
        approval_sha256=_digest(approval_json),
        context_sha256=_context_sha256(session),
        cycle_number=session.cycle_number,
        planned_paths=tuple(planned),
        files=files,
        issues=tuple(dict.fromkeys(issues)),
    )


def approval_changes(
    session: AgentSession, approval: ApprovalRequest, snapshot: ApprovalSnapshot | None,
) -> list[str]:
    """执行前只核对同一份审批快照；发生变化须重新展示方案，不能自动重签。"""
    if snapshot is None:
        return ["历史审批缺少文件依据快照，请重新生成并审阅方案。"]
    changes = list(snapshot.issues)
    current_approval = _canonical(approval.model_dump(mode="json"))
    if (_digest(snapshot.approval_json) != snapshot.approval_sha256
            or current_approval != snapshot.approval_json):
        changes.append("待审方案内容已变化，请重新生成并审阅方案。")
    if (snapshot.cycle_number != session.cycle_number
            or snapshot.context_sha256 != _context_sha256(session)):
        changes.append("任务需求或调查上下文已变化，请重新分析并审阅方案。")
    changes.extend(approval_file_changes(session, snapshot))
    return list(dict.fromkeys(changes))


def approval_file_changes(session: AgentSession, snapshot: ApprovalSnapshot) -> list[str]:
    """紧邻首次实施启动前重查文件，允许宿主已追加授权消息而不改签上下文。"""
    current = _fingerprints(session.workspace, (item.path for item in snapshot.files))
    return [
        f"审批依据文件「{before.path}」已变化或无法核对。"
        for before, after in zip(snapshot.files, current, strict=True)
        if before != after or after.state == "unverified"
    ]


def audit_implementation(
    session: AgentSession,
    snapshot: ApprovalSnapshot,
    command_id: str,
    reported_paths: Iterable[str] = (),
    interrupted: bool = False,
) -> ImplementationReport:
    """对照真实前后指纹及本命令工具记录；保留不确定副作用，供只读恢复核查。"""
    planned = set(snapshot.planned_paths)
    before = {item.path: item for item in snapshot.files}
    current = {
        item.path: item for item in _fingerprints(session.workspace, before)
    }
    changed, unchanged, outside, unverified = set(), set(), set(), set()
    for path, old in before.items():
        new = current[path]
        if old.state == "unverified" or new.state == "unverified":
            unverified.add(path)
        elif old != new:
            (changed if path in planned else outside).add(path)
        elif path in planned:
            unchanged.add(path)

    operations: dict[tuple[str | None, str], dict[str, Any]] = {}
    last_finished: dict[str, str | None] = {}
    for event in session.events:
        data = event.data
        if (event.command_id != command_id
                or event.type.value not in {"tool_started", "tool_finished"}
                or str(data.get("tool_name", "")).casefold() not in {"write", "edit"}):
            continue
        raw_path = str(data.get("path") or "")
        path = normalized_path(session.workspace, raw_path)
        label = path or raw_path or "未记录路径的写操作"
        if path not in planned:
            outside.add(label)
        if path is None:
            unverified.add(label)
        key = (event.correlation_id, str(data.get("tool_use_id") or event.id))
        item = operations.setdefault(key, {"path": label, "finished": False})
        if event.type.value == "tool_finished":
            item.update(finished=True, is_error=data.get("is_error", True),
                        sha256=data.get("content_sha256"))
            last_finished[label] = data.get("content_sha256")
    for item in operations.values():
        path = item["path"]
        if not item["finished"] or item.get("is_error"):
            unverified.add(path)
    # 同一文件多次正常修改只核对最后一次完成指纹，避免旧写入被误判为漂移。
    for path, sha256 in last_finished.items():
        if path in current and (not sha256 or current[path].sha256 != sha256):
            unverified.add(path)

    reported_only = set()
    for reference in reported_paths:
        path = normalized_path(session.workspace, reference)
        if path not in changed:
            reported_only.add(path or reference)
    return ImplementationReport(
        snapshot_id=snapshot.id,
        command_id=command_id,
        changed_paths=tuple(sorted(changed)),
        unchanged_paths=tuple(sorted(unchanged)),
        out_of_scope_paths=tuple(sorted(outside)),
        unverified_paths=tuple(sorted(unverified)),
        reported_only_paths=tuple(sorted(reported_only)),
        interrupted=interrupted,
        limitations=(
            "仅核对方案文件、读取依据与已观察写操作，未扫描整个工作区。",
            "文件变化不证明由 ACE 独占产生，也不证明功能验证通过。",
        ),
    )


def report_text(report: ImplementationReport) -> str:
    """统一 UI 与恢复上下文所用的简明文件核对摘要。"""
    lines = [f"### 执行核对 · 方案 {report.snapshot_id[:8]}"]
    if report.interrupted:
        lines.append("执行已中断；先核对已保存结果，再继续剩余步骤。")
    for label, paths in (
        ("已观察到计划内变化", report.changed_paths),
        ("计划内未变化", report.unchanged_paths),
        ("范围外写尝试或变化", report.out_of_scope_paths),
        ("结果尚未核实", report.unverified_paths),
        ("仅模型报告，尚无文件变化证据", report.reported_only_paths),
    ):
        if paths:
            lines.append(f"\n**{label}（{len(paths)}）**")
            lines.extend(f"- `{path}`" for path in paths[:20])
            if len(paths) > 20:
                lines.append(f"- 另有 {len(paths) - 20} 项，详见任务核对记录。")
    if not any((report.changed_paths, report.unchanged_paths, report.out_of_scope_paths,
                report.unverified_paths, report.reported_only_paths)):
        lines.append("本次没有可核对的文件改动记录。")
    lines.append("\n" + " ".join(report.limitations))
    return "\n".join(lines)
