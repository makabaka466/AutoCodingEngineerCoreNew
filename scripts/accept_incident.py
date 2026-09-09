"""手动真实验收异常流程：复用本机配置，运行记录只写入 outputs，不打印凭据或数据行。

例：python scripts/accept_incident.py --workspace <测试项目> --data-dir outputs/incident-smoke
    --message "页面发生了什么问题"。续聊增加 --session <会话 ID>，需要访问已配置模型和数据库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from autocoding_agent.adapters.process_options import hidden_window_options
from autocoding_agent.config import Settings
from autocoding_agent.incident.application import build_incident_application
from autocoding_agent.model_setup import ClaudeModelSetupService, UserEnvironmentStore
from autocoding_agent.sqlserver_service import SQLServerConnectionService


def workspace_fingerprint(workspace: Path) -> dict[str, str]:
    """只比对 Git 状态及已跟踪差异，不输出业务源码，也不清理用户工作区。"""
    result = {}
    for name, args in {
        "status": ["status", "--porcelain"],
        "diff": ["diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"],
    }.items():
        check = subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True,
            check=True, timeout=60, **hidden_window_options(),
        )
        result[name] = hashlib.sha256(check.stdout).hexdigest()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--audit-git-dir", type=Path, default=Path("."),
                        help="工作区内的 Git 仓库相对目录，只用于前后差异核验")
    parser.add_argument("--message", required=True)
    parser.add_argument("--session")
    parser.add_argument("--project", default="生物")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    data_dir = args.data_dir.resolve()
    output_root = Path(__file__).resolve().parents[1] / "outputs"
    if not data_dir.is_relative_to(output_root) or data_dir == output_root:
        parser.error("验收数据目录必须为本仓库 outputs 下的独立子目录。")
    workspace = args.workspace.resolve(strict=True)
    audit_root = (workspace / args.audit_git_dir).resolve(strict=True)
    if not audit_root.is_relative_to(workspace):
        parser.error("Git 核验目录必须位于本次工作区内。")

    # 仅将已保存配置同步到当前测试进程，不修改用户注册表或显示 API Key。
    environment = UserEnvironmentStore()
    for key in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL", "AUTO_CODING_CLAUDE_MODEL", "AUTO_CODING_CLAUDE_COMMAND",
    ):
        if value := environment.get(key):
            os.environ[key] = value
    state = ClaudeModelSetupService().inspect()
    if not state.ready:
        raise RuntimeError("请先在 ACE 配置页面完成模型配置。")
    configured = Settings()
    reader = SQLServerConnectionService(settings=configured).reader()
    if reader is None:
        raise RuntimeError("请先在 ACE 配置页面完成 SQL Server 配置。")
    before = workspace_fingerprint(audit_root)
    application = build_incident_application(
        settings=configured.model_copy(update={"data_dir": data_dir}),
        database=reader, database_reference=reader.reference,
    )
    print(json.dumps({"model": configured.claude_model, "data_dir": str(data_dir)}), flush=True)
    progress = lambda event: print(  # noqa: E731
        json.dumps({"phase": event.phase.value}, ensure_ascii=False), flush=True,
    )
    started = time.monotonic()
    previous_usage = {}
    previous_runs = 0
    if args.session:
        previous = application.get_session(args.session)
        if Path(previous.workspace) != workspace:
            parser.error("续聊会话的工作区与 --workspace 不一致。")
        previous_usage = previous.last_usage.model_dump()
        previous_runs = len(previous.runs)
        outcome = application.send(args.session, args.message, progress_sink=progress)
    else:
        outcome = application.start(
            workspace, args.message, project=args.project, progress_sink=progress,
        )
    session = application.get_session(outcome.session_id)
    summary = {
        "session_id": outcome.session_id, "cycle": outcome.cycle_number,
        "status": outcome.status.value, "completion_kind": outcome.completion_kind.value,
        "message": outcome.message, "question": outcome.question,
        "page": outcome.page.model_dump(mode="json") if outcome.page else None,
        "usage": outcome.usage.model_dump(mode="json"),
        "usage_scope": "session_cumulative_reported_usage; failed CLI runs may be absent",
        "command_usage_delta": {
            key: value - previous_usage.get(key, 0)
            for key, value in outcome.usage.model_dump().items()
            if isinstance(value, (int, float))
        },
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "queries": [{"name": o.query_name, "stage": o.stage, "rows": o.returned_rows,
                     "status": o.status.value} for o in outcome.query_observations],
        "runs": [r.status.value for r in session.runs],
        "new_runs": [r.status.value for r in session.runs[previous_runs:]],
        "queries_scope": "current_cycle_cumulative",
        "git_status_and_tracked_diff_unchanged": workspace_fingerprint(audit_root) == before,
        "capability_document": outcome.capability_document,
    }
    # 完整快照、事件及产物已由应用保存；摘要仅用于人工核验，不上传 GitHub。
    report = data_dir / (
        f"acceptance-{session.id}-cycle-{session.cycle_number}-run-{len(session.runs)}.json"
    )
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
