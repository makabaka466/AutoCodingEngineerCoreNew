"""各交互端共用的方案排版；只组织显示，不改写模型的修改内容。"""

from autocoding_agent.core.approval_execution_models import ApprovalSnapshot
from autocoding_agent.core.models import ApprovalRequest


def format_approval_details(
    approval: ApprovalRequest, snapshot: ApprovalSnapshot | None = None,
) -> str:
    """将修改位置、前后行为和验收分段展示，保留代码块及原始换行。"""
    sections: list[str] = []
    if snapshot is not None:
        sections.extend([
            f"方案编号：`{snapshot.id[:8]}`",
            "批准前会重新核对方案依据和目标文件；发生变化时，需要重新审阅。",
        ])
        if snapshot.planned_paths:
            sections.extend([
                "**本次批准范围**",
                "\n\n".join(f"- `{path}`" for path in snapshot.planned_paths),
            ])
        if snapshot.issues:
            sections.extend(["**需先调整方案**", "\n\n".join(
                f"- {issue}" for issue in snapshot.issues
            )])
    proposal = approval.proposal
    if proposal is None:
        sections.append(approval.reason)
        if approval.proposed_actions:
            sections.extend(["## 计划操作", "\n\n".join(
                f"- {item}" for item in approval.proposed_actions
            )])
        return "\n\n".join(sections)

    sections.extend(["## 方案概述", proposal.summary, "## 修改内容"])
    for index, change in enumerate(proposal.changes, start=1):
        sections.append(f"### {index}. {change.area}")
        if change.path:
            sections.append(f"文件：`{change.path}`")
        sections.extend(["**修改前**", change.current, "**修改后**", change.proposed])
    sections.extend(["## 目标效果", proposal.expected_result])
    if proposal.impact:
        sections.extend(["## 影响与边界", "\n\n".join(f"- {item}" for item in proposal.impact)])
    if proposal.validation:
        sections.extend(["## 验证计划", "\n\n".join(
            f"{index}. {item}" for index, item in enumerate(proposal.validation, start=1)
        )])
    preview_fallback = (
        "暂无可在实施前可靠呈现的预览，将按上面的验证计划确认最终效果。"
        if proposal.validation
        else "这项修改不适合在实施前生成可信预览，实施后再确认实际效果。"
    )
    sections.extend(["## 预览", proposal.preview_markdown or preview_fallback])
    return "\n\n".join(sections)
