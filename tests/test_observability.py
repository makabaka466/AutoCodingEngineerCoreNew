from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autocoding_agent.adapters.claude_code import ClaudeCodeError, ClaudeCodeRuntime
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentMode, RuntimeTurn
from autocoding_agent.observability import configure_file_logging


def test_timeout_is_written_to_rotating_local_log_without_prompt(tmp_path: Path) -> None:
    log_path = configure_file_logging(tmp_path / "state")
    settings = Settings(
        claude_command="D:/claude/claude.exe",
        claude_model="test-model",
        claude_timeout_seconds=10,
        data_dir=tmp_path / "state",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    turn = RuntimeTurn(
        session_id="14b516f5-a4aa-499f-91fc-48e0a19a5b4e",
        workspace=str(workspace),
        user_message="PRIVATE USER QUESTION",
        mode=AgentMode.INSPECT,
        system_prompt="PRIVATE SYSTEM PROMPT",
        tools=["Read"],
        allowed_tools=["Read"],
    )

    def timeout_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], timeout=10)

    runtime = ClaudeCodeRuntime(settings, runner=timeout_runner)
    with pytest.raises(ClaudeCodeError, match="exceeded"):
        runtime.run(turn)

    content = log_path.read_text(encoding="utf-8")
    assert "turn_timeout" in content
    assert turn.session_id in content
    assert "timeout_seconds=10" in content
    assert "PRIVATE USER QUESTION" not in content
    assert "PRIVATE SYSTEM PROMPT" not in content
