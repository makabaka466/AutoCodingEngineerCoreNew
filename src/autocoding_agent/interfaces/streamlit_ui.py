"""A Codex-like chat surface backed entirely by AgentApplication."""

from __future__ import annotations

import sys
from pathlib import Path

from autocoding_agent.application import AgentApplication, build_application
from autocoding_agent.core.models import AgentStatus, ApprovalScope, MessageRole
from autocoding_agent.core.progress import PROGRESS_LABELS, ProgressEvent
from autocoding_agent.core.workflow import RESULT_LABELS, assessment_text


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="AutoCoding Engineer", page_icon="◈", layout="wide")
    st.markdown(
        """<style>
        .block-container {max-width: 960px; padding-top: 2rem;}
        [data-testid="stChatMessage"] {border-radius: 14px; padding: .35rem .75rem;}
        </style>""",
        unsafe_allow_html=True,
    )
    if "agent_application" not in st.session_state:
        st.session_state.agent_application = build_application()
    if "session_id" not in st.session_state:
        st.session_state.session_id = None

    application: AgentApplication = st.session_state.agent_application
    with st.sidebar:
        st.title("AutoCoding Engineer")
        workspace = st.text_input("项目路径", value=st.session_state.get("workspace", ""))
        st.session_state.workspace = workspace
        if st.button("新建任务", use_container_width=True):
            st.session_state.session_id = None
            st.session_state.git_preview = None
            st.rerun()
        if st.button("更新 Git 代码", use_container_width=True):
            try:
                current = (
                    application.get_session(st.session_state.session_id)
                    if st.session_state.session_id else None
                )
                if current and current.pending_approval:
                    st.error("请先拒绝当前审批方案，再更新代码并重新调查。")
                else:
                    with st.spinner("正在更新代码…"):
                        head = application.sync_git(current.workspace if current else workspace)
                    st.session_state.git_preview = None
                    st.success(f"已更新到 {head[:12]}")
            except Exception as exc:
                st.error(str(exc))
        if st.session_state.session_id:
            st.caption(f"Session\n{st.session_state.session_id}")

    st.title("软件开发 Agent")
    st.caption("模型负责理解、调查和方案判断；程序只负责会话、权限与持久化边界。")

    session = None
    if st.session_state.session_id:
        try:
            session = application.get_session(st.session_state.session_id)
        except Exception as exc:
            st.error(str(exc))
            st.session_state.session_id = None

    if session:
        for item in session.messages:
            role = "user" if item.role == MessageRole.USER else "assistant"
            with st.chat_message(role):
                st.markdown(item.content)

        decision = session.last_decision
        if decision and decision.assessment:
            assessment = decision.assessment
            label = (
                RESULT_LABELS[assessment.result]
                if session.status == AgentStatus.COMPLETED
                else PROGRESS_LABELS[assessment.phase]
            )
            with st.container(border=True):
                st.markdown(f"**当前状态：{label}**")
                st.text(assessment_text(assessment))

    # 位于对话下方，回调只投影内核事件；Streamlit 自有 spinner 表达真实执行等待。
    progress_placeholder = st.empty()

    def show_progress(event: ProgressEvent) -> None:
        progress_placeholder.info(
            event.label + (f" · {event.detail}" if event.detail else "")
        )

    if session:

        if session.pending_approval:
            approval = session.pending_approval
            st.warning(f"需要授权：{approval.reason}")
            proposal = approval.proposal
            if proposal:
                with st.container(border=True):
                    st.markdown("#### 修改方案")
                    st.markdown(proposal.summary)
                    st.markdown("**修改内容**")
                    for index, change in enumerate(proposal.changes, start=1):
                        location = change.path or change.area
                        if change.path and change.area != change.path:
                            location = f"{change.path} · {change.area}"
                        st.markdown(f"**{index}. {location}**")
                        st.markdown(f"- 现在：{change.current}\n- 改成：{change.proposed}")
                    st.markdown(f"**目标效果**\n\n{proposal.expected_result}")
                    st.markdown("**预览**")
                    preview_fallback = (
                        "暂无可在实施前可靠呈现的预览，将按下面的验证计划确认最终效果。"
                        if proposal.validation
                        else "这项修改不适合在实施前生成可信预览，实施后再确认实际效果。"
                    )
                    st.markdown(proposal.preview_markdown or preview_fallback)
                    if proposal.impact:
                        st.markdown(
                            "**影响与边界**\n\n"
                            + "\n".join(f"- {item}" for item in proposal.impact)
                        )
                    if proposal.validation:
                        st.markdown(
                            "**验证计划**\n\n"
                            + "\n".join(f"- {item}" for item in proposal.validation)
                        )
            elif approval.proposed_actions:
                st.markdown("\n".join(f"- {item}" for item in approval.proposed_actions))
            legacy_modify = approval.scope == ApprovalScope.MODIFY and proposal is None
            if legacy_modify:
                st.info("此审批来自旧会话，缺少修改方案。请拒绝并要求 Agent 重新生成方案。")
            reason = st.text_input("希望调整的内容（可选）", key="reject_reason")
            approve_col, reject_col = st.columns(2)
            approve_label = (
                "需要重新生成"
                if legacy_modify
                else "批准此方案"
                if proposal
                else "批准并继续"
            )
            if approve_col.button(
                approve_label,
                type="primary",
                use_container_width=True,
                disabled=legacy_modify,
            ):
                with st.spinner("Claude Code 正在继续任务…"):
                    application.approve(session.id, progress_sink=show_progress)
                st.rerun()
            if reject_col.button("拒绝或要求调整", use_container_width=True):
                with st.spinner("正在继续只读处理…"):
                    application.reject(session.id, reason, progress_sink=show_progress)
                st.rerun()

        if session.status == AgentStatus.COMPLETED:
            st.success(f"第 {session.cycle_number} 轮任务已结束，可继续追问")
            if session.capability_document:
                st.caption(f"能力文档：{session.capability_document}")
            st.info("可以在下方继续追问或补充要求；也可以点击左侧“新建任务”。")
            if st.button("预览待推送修改"):
                try:
                    st.session_state.git_preview = application.git_preview(session.id)
                except Exception as exc:
                    st.error(str(exc))
            preview = st.session_state.get("git_preview")
            if preview:
                st.write(f"目标：{preview['remote']} / {preview['branch']}")
                st.write("涉及文件：", preview["files"])
                if preview["pending_push"]:
                    st.info("本地已有一个提交，确认后仅重试推送。")
                summary = st.text_input(
                    "简短修改内容", value=preview.get("summary", ""), key="git_summary",
                )
                reviewed = st.checkbox("我已检查目标分支和以上文件")
                if st.button("提交并推送", disabled=not reviewed or not summary.strip()):
                    try:
                        with st.spinner("正在提交并推送…"):
                            result = application.git_publish(
                                session.id, summary, preview["fingerprint"],
                            )
                        st.session_state.git_preview = None
                        st.success(f"已推送提交 {result['commit'][:12]}")
                    except Exception as exc:
                        st.error(str(exc))

    prompt = st.chat_input(
        "继续追问，或者补充新的修改要求…"
        if session is not None and session.status == AgentStatus.COMPLETED
        else "描述要调查或实现的开发任务…"
    )
    if prompt:
        if not st.session_state.session_id and not workspace.strip():
            st.error("请先填写项目路径。")
            return
        with st.spinner("Claude Code 正在处理…"):
            try:
                if st.session_state.session_id:
                    outcome = application.send(
                        st.session_state.session_id, prompt, progress_sink=show_progress
                    )
                else:
                    outcome = application.start(workspace, prompt, progress_sink=show_progress)
                    st.session_state.session_id = outcome.session_id
                    st.session_state.git_preview = None
            except Exception as exc:
                st.error(str(exc))
                return
        st.rerun()


def run() -> None:
    """Console entry point that starts Streamlit with this file."""

    from streamlit.web import cli as streamlit_cli

    sys.argv = ["streamlit", "run", str(Path(__file__).resolve())]
    raise SystemExit(streamlit_cli.main())


if __name__ == "__main__":
    main()
