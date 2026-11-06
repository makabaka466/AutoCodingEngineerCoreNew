"""Web 审批按钮必须对应用户实际看到的方案版本。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_flows import _approval

from autocoding_agent.core.models import AgentSession, AgentStatus, ApprovalScope
from autocoding_agent.interfaces import streamlit_ui


def test_old_approval_click_cannot_approve_reloaded_proposal(tmp_path):
    testing = pytest.importorskip("streamlit.testing.v1")
    session = AgentSession(
        id="web-review", workspace=str(tmp_path), goal="修复库存问题", version=4,
        status=AgentStatus.APPROVAL_REQUIRED,
        pending_approval=_approval(ApprovalScope.MODIFY).approval,
    )
    approved_versions = []
    application = SimpleNamespace(
        get_session=lambda _session_id: session,
        approve=lambda _session_id, **kwargs: approved_versions.append(kwargs["expected_version"]),
    )
    page = testing.AppTest.from_file(Path(streamlit_ui.__file__))
    page.session_state["agent_application"] = application
    page.session_state["session_id"] = session.id
    page.run()
    assert not page.exception

    # 用户点击旧页面时，其他窗口已提交新方案；本次重跑只能展示新版。
    session.version = 5
    page.button(key="approve:web-review:4").click().run()
    assert not page.exception
    assert approved_versions == []

    page.button(key="approve:web-review:5").click().run()
    assert not page.exception
    assert approved_versions == [5]
