"""A Codex-like chat surface backed entirely by AgentApplication."""

from __future__ import annotations

import sys
from pathlib import Path

from autocoding_agent.application import AgentApplication, build_application
from autocoding_agent.core.models import AgentStatus, MessageRole


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
            st.rerun()
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

        if session.pending_approval:
            approval = session.pending_approval
            st.warning(f"需要授权：{approval.reason}")
            if approval.proposed_actions:
                st.markdown("\n".join(f"- {item}" for item in approval.proposed_actions))
            reason = st.text_input("拒绝原因（可选）", key="reject_reason")
            approve_col, reject_col = st.columns(2)
            if approve_col.button("批准并继续", type="primary", use_container_width=True):
                with st.spinner("Claude Code 正在继续任务…"):
                    application.approve(session.id)
                st.rerun()
            if reject_col.button("拒绝", use_container_width=True):
                with st.spinner("正在继续只读处理…"):
                    application.reject(session.id, reason)
                st.rerun()

        if session.status == AgentStatus.COMPLETED:
            st.success("任务已完成")
            if session.capability_document:
                st.caption(f"能力文档：{session.capability_document}")
            st.info("点击左侧“新建任务”开始下一项工作。")
            return

    prompt = st.chat_input("描述要调查或实现的开发任务…")
    if prompt:
        if not st.session_state.session_id and not workspace.strip():
            st.error("请先填写项目路径。")
            return
        with st.spinner("Claude Code 正在处理…"):
            try:
                if st.session_state.session_id:
                    outcome = application.send(st.session_state.session_id, prompt)
                else:
                    outcome = application.start(workspace, prompt)
                    st.session_state.session_id = outcome.session_id
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
