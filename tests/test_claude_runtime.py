"""Contract tests for the Claude Code CLI adapter."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from autocoding_agent.adapters.claude_code import ClaudeCodeError, ClaudeCodeRuntime
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentMode, RuntimeTurn
from autocoding_agent.incident.models import IncidentDecision, IncidentStatus


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "claude_command": "D:/claude/claude.exe",
        "claude_model": "deepseek-test",
        "claude_timeout_seconds": 45,
        "max_budget_usd": 1.25,
        "data_dir": tmp_path / "state",
    }
    values.update(overrides)
    return Settings(**values)


def _turn(tmp_path: Path, *, runtime_session_id: str | None = None) -> RuntimeTurn:
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    capability_dir = tmp_path / "memory"
    capability_dir.mkdir(exist_ok=True)
    return RuntimeTurn(
        session_id="f5b7a834-f94d-45ae-a0f1-6bc16ebf59ae",
        runtime_session_id=runtime_session_id,
        workspace=str(workspace),
        user_message="Inspect upload behavior.",
        mode=AgentMode.INSPECT,
        system_prompt="SYSTEM PROMPT",
        tools=["Read", "Glob", "Grep"],
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="dontAsk",
        capability_dir=str(capability_dir),
    )


def _option_value(command: list[str], option: str) -> str:
    return command[command.index(option) + 1]


def test_build_command_contains_structured_contract_and_new_session_id(tmp_path: Path) -> None:
    runtime = ClaudeCodeRuntime(_settings(tmp_path))
    turn = _turn(tmp_path)

    command = runtime.build_command(turn)

    assert command[0] == "D:/claude/claude.exe"
    assert command[-1] == "Inspect upload behavior."
    assert "--bare" in command
    assert "--no-chrome" in command
    assert "--strict-mcp-config" in command
    assert _option_value(command, "--mcp-config") == '{"mcpServers":{}}'
    assert _option_value(command, "--setting-sources") == ""
    assert _option_value(command, "--output-format") == "json"
    assert _option_value(command, "--model") == "deepseek-test"
    assert _option_value(command, "--permission-mode") == "dontAsk"
    assert _option_value(command, "--tools") == "Read,Glob,Grep"
    allowed_tools = command[
        command.index("--allowedTools") + 1 : command.index("--append-system-prompt")
    ]
    assert allowed_tools == [
        "Read",
        "Glob",
        "Grep",
    ]
    assert _option_value(command, "--append-system-prompt") == "SYSTEM PROMPT"
    assert _option_value(command, "--add-dir") == turn.capability_dir
    assert _option_value(command, "--max-budget-usd") == "1.25"
    assert _option_value(command, "--session-id") == turn.session_id
    assert "--resume" not in command
    schema = json.loads(_option_value(command, "--json-schema"))
    assert "status" in schema["properties"]
    assert "message" in schema["properties"]
    proposal_schema = schema["$defs"]["ChangeProposal"]
    change_schema = schema["$defs"]["ProposedChange"]
    approval_schema = schema["$defs"]["ApprovalRequest"]
    assert set(proposal_schema["required"]) == {"summary", "changes", "expected_result"}
    assert proposal_schema["properties"]["changes"]["minItems"] == 1
    assert "preview_markdown" not in proposal_schema["required"]
    assert set(change_schema["required"]) == {"area", "current", "proposed"}
    assert "proposal" in approval_schema["required"]


def test_build_command_resumes_exact_runtime_session(tmp_path: Path) -> None:
    runtime = ClaudeCodeRuntime(_settings(tmp_path, max_budget_usd=None))
    turn = _turn(tmp_path, runtime_session_id="claude-runtime-session")

    command = runtime.build_command(turn)

    assert _option_value(command, "--resume") == "claude-runtime-session"
    assert "--session-id" not in command
    assert "--max-budget-usd" not in command


def test_generic_structured_runtime_uses_incident_schema(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "session_id": "runtime-incident",
                    "structured_output": {
                        "status": "needs_input",
                        "message": "Need the affected route.",
                        "question": "Which page shows the problem?",
                    },
                }
            ),
            stderr="",
        )

    runtime = ClaudeCodeRuntime(_settings(tmp_path), runner=runner)

    result = runtime.run_structured(_turn(tmp_path), IncidentDecision)

    assert result.output.status == IncidentStatus.NEEDS_INPUT
    assert result.runtime_session_id == "runtime-incident"
    command = captured["command"]
    assert isinstance(command, list)
    schema = json.loads(_option_value(command, "--json-schema"))
    assert "queries" in schema["properties"]
    assert "diagnosis" in schema["properties"]


@pytest.mark.parametrize(
    ("stdout", "error_fragment"),
    [
        ("not json", "valid JSON result envelope"),
        (json.dumps({"session_id": "runtime-1"}), "without the structured result"),
        (
            json.dumps(
                {
                    "is_error": True,
                    "subtype": "error_during_execution",
                    "result": "provider failed",
                    "session_id": "runtime-1",
                    "structured_output": {
                        "status": "completed",
                        "message": "This must not be accepted.",
                    },
                }
            ),
            "provider failed",
        ),
        (
            json.dumps(
                {
                    "session_id": "runtime-1",
                    "structured_output": {"status": "completed"},
                }
            ),
            "invalid agent decision",
        ),
        (
            json.dumps(
                {
                    "session_id": "runtime-1",
                    "structured_output": {
                        "status": "approval_required",
                        "message": "Need approval but omitted the request.",
                    },
                }
            ),
            "invalid agent decision",
        ),
        (
            json.dumps(
                {
                    "structured_output": {
                        "status": "completed",
                        "message": "Done.",
                    }
                }
            ),
            "resumable session id",
        ),
    ],
    ids=[
        "invalid-envelope-json",
        "missing-structured-output",
        "error-result-envelope",
        "missing-message",
        "approval-without-request",
        "missing-session-id",
    ],
)
def test_invalid_structured_results_are_rejected(
    tmp_path: Path,
    stdout: str,
    error_fragment: str,
) -> None:
    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

    runtime = ClaudeCodeRuntime(_settings(tmp_path), runner=runner)

    with pytest.raises(ClaudeCodeError, match=error_fragment):
        runtime.run(_turn(tmp_path))


def test_runtime_invocation_uses_workspace_timeout_and_utf8(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="not json", stderr="")

    runtime = ClaudeCodeRuntime(_settings(tmp_path), runner=runner)

    with pytest.raises(ClaudeCodeError):
        runtime.run(_turn(tmp_path))

    assert captured["cwd"] == _turn(tmp_path).workspace
    assert captured["timeout"] == 45
    assert captured["encoding"] == "utf-8"
    if os.name == "nt":
        assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW
        startupinfo = captured["startupinfo"]
        assert isinstance(startupinfo, subprocess.STARTUPINFO)
        assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW


def test_runtime_error_redacts_provider_credentials(tmp_path: Path) -> None:
    secret = "sk-1234567890abcdefghijkl"

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr=f"Authorization: Bearer {secret} api_key={secret}",
        )

    runtime = ClaudeCodeRuntime(_settings(tmp_path), runner=runner)

    with pytest.raises(ClaudeCodeError) as error:
        runtime.run(_turn(tmp_path))

    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)
