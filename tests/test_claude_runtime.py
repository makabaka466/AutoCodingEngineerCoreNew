"""Contract tests for the Claude Code CLI adapter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from autocoding_agent.adapters.claude_code import (
    ClaudeCodeError,
    ClaudeCodeRuntime,
    _command_line_chars,
    _validate_command_line_length,
)
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentMode, RuntimeTurn
from autocoding_agent.core.runtime.models import RuntimeEventKind
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
    prompt_file = tmp_path / "system-prompt.md"
    prompt_file.write_text(turn.system_prompt, encoding="utf-8")

    command = runtime.build_command(turn, system_prompt_file=prompt_file)

    assert command[0] == "D:/claude/claude.exe"
    assert turn.user_message not in command
    assert "--safe-mode" in command
    assert "--bare" not in command
    assert "--no-chrome" in command
    assert "--strict-mcp-config" in command
    assert _option_value(command, "--mcp-config") == '{"mcpServers":{}}'
    assert _option_value(command, "--setting-sources") == ""
    assert _option_value(command, "--output-format") == "json"
    assert _option_value(command, "--input-format") == "text"
    assert _option_value(command, "--model") == "deepseek-test"
    assert _option_value(command, "--permission-mode") == "dontAsk"
    assert _option_value(command, "--tools") == "Read,Glob,Grep"
    allowed_tools = command[
        command.index("--allowedTools") + 1 : command.index("--append-system-prompt-file")
    ]
    assert allowed_tools == [
        "Read",
        "Glob",
        "Grep",
    ]
    assert "--append-system-prompt" not in command
    assert _option_value(command, "--append-system-prompt-file") == str(prompt_file)
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
    prompt_file = tmp_path / "system-prompt.md"
    prompt_file.write_text(turn.system_prompt, encoding="utf-8")

    command = runtime.build_command(turn, system_prompt_file=prompt_file)

    assert _option_value(command, "--resume") == "claude-runtime-session"
    assert "--session-id" not in command
    assert "--max-budget-usd" not in command


def test_build_command_mounts_capability_and_isolated_attachment_directories(
    tmp_path: Path,
) -> None:
    runtime = ClaudeCodeRuntime(_settings(tmp_path))
    turn = _turn(tmp_path)
    first = tmp_path / "attachments" / "one"
    second = tmp_path / "attachments" / "two"
    turn.additional_dirs = [str(first), str(second), str(first)]
    prompt_file = tmp_path / "system-prompt.md"
    prompt_file.write_text(turn.system_prompt, encoding="utf-8")

    command = runtime.build_command(turn, system_prompt_file=prompt_file)
    mounted = [
        command[index + 1]
        for index, item in enumerate(command)
        if item == "--add-dir"
    ]

    assert mounted == [turn.capability_dir, str(first), str(second)]


def test_generic_structured_runtime_uses_incident_schema(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
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
    assert captured["input"] == "Inspect upload behavior."
    assert "Inspect upload behavior." not in command
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
    assert captured["input"] == "Inspect upload behavior."
    assert captured["timeout"] == 45
    assert captured["encoding"] == "utf-8"
    if os.name == "nt":
        assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW
        startupinfo = captured["startupinfo"]
        assert isinstance(startupinfo, subprocess.STARTUPINFO)
        assert startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW


def test_large_system_prompt_uses_temporary_file_and_stays_out_of_argv(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    turn = _turn(tmp_path)
    turn.system_prompt = "DATABASE_SCHEMA\n" + ("column_name nvarchar(200)\n" * 4_000)

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        prompt_path = Path(_option_value(command, "--append-system-prompt-file"))
        captured["prompt_path"] = prompt_path
        captured["command_chars"] = _command_line_chars(command)
        assert prompt_path.read_text(encoding="utf-8") == turn.system_prompt
        assert turn.system_prompt not in command
        assert turn.user_message not in command
        assert kwargs["input"] == turn.user_message
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "session_id": "runtime-large-prompt",
                    "structured_output": {"status": "completed", "message": "Done."},
                }
            ),
            stderr="",
        )

    runtime = ClaudeCodeRuntime(_settings(tmp_path), runner=runner)

    result = runtime.run(turn)

    assert result.runtime_session_id == "runtime-large-prompt"
    assert int(captured["command_chars"]) < 32_767
    prompt_path = captured["prompt_path"]
    assert isinstance(prompt_path, Path)
    assert not prompt_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcess limit only")
def test_windows_command_line_preflight_rejects_oversized_residual_arguments() -> None:
    with pytest.raises(ClaudeCodeError, match="Windows 命令行长度限制"):
        _validate_command_line_length(["claude.exe", "x" * 32_767])


def test_real_runtime_recovers_a_stale_configured_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    recovered.touch()
    runtime = ClaudeCodeRuntime(_settings(tmp_path, claude_command="C:/stale/claude.exe"))
    monkeypatch.setattr(
        "autocoding_agent.adapters.claude_code.resolve_claude_command",
        lambda _configured: str(recovered),
    )
    turn = _turn(tmp_path)

    prepared = runtime._prepare_launch_command(
        [runtime.settings.claude_command, "--version"],
        turn,
        validate=True,
    )

    assert prepared == [str(recovered), "--version"]


def test_real_runtime_reports_missing_workspace_separately(tmp_path: Path) -> None:
    runtime = ClaudeCodeRuntime(_settings(tmp_path, claude_command=sys.executable))
    turn = _turn(tmp_path)
    Path(turn.workspace).rmdir()

    with pytest.raises(ClaudeCodeError, match="项目目录不存在或不可访问"):
        runtime._prepare_launch_command(
            [runtime.settings.claude_command, "--version"],
            turn,
            validate=True,
        )


def test_real_runtime_reports_missing_executable_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing-claude.exe"
    runtime = ClaudeCodeRuntime(_settings(tmp_path, claude_command=str(missing)))
    monkeypatch.setattr(
        "autocoding_agent.adapters.claude_code.resolve_claude_command",
        lambda _configured: str(missing),
    )

    with pytest.raises(ClaudeCodeError, match="Claude Code 程序不存在或不可访问"):
        runtime._prepare_launch_command(
            [runtime.settings.claude_command, "--version"],
            _turn(tmp_path),
            validate=True,
        )


def test_runtime_reports_other_process_start_errors(tmp_path: Path) -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("access denied")

    runtime = ClaudeCodeRuntime(_settings(tmp_path), runner=runner)

    with pytest.raises(ClaudeCodeError, match="Claude Code 进程启动失败：access denied"):
        runtime.run(_turn(tmp_path))


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


def test_observed_runtime_parses_sanitized_tool_lifecycle(tmp_path: Path) -> None:
    secret = "sk-1234567890abcdefghijkl"
    stream = [
        {"type": "system", "subtype": "init", "model": "deepseek-test"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": f"api_key={secret} python -m pytest -q"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "102 passed",
                        "is_error": False,
                    }
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": "runtime-stream",
            "structured_output": {"status": "completed", "message": "Done."},
            "usage": {"input_tokens": 3, "output_tokens": 4},
        },
    ]
    script = "import sys\n" + "\n".join(
        f"print({json.dumps(json.dumps(item))}, flush=True)" for item in stream
    )
    captured: dict[str, object] = {}

    def popen_factory(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
        captured["command"] = command
        captured["prompt_path"] = Path(
            _option_value(command, "--append-system-prompt-file")
        )
        captured["stdin"] = kwargs.get("stdin")
        assert captured["prompt_path"].read_text(encoding="utf-8") == "SYSTEM PROMPT"
        return subprocess.Popen([sys.executable, "-c", script], **kwargs)

    runtime = ClaudeCodeRuntime(_settings(tmp_path), popen_factory=popen_factory)
    activities = []

    result = runtime.run_observed(_turn(tmp_path), "run-1", activities.append)

    assert result.runtime_session_id == "runtime-stream"
    assert result.usage.input_tokens == 3
    assert [item.kind for item in activities] == [
        RuntimeEventKind.SYSTEM_INIT,
        RuntimeEventKind.TOOL_STARTED,
        RuntimeEventKind.TOOL_FINISHED,
    ]
    assert secret not in json.dumps([item.model_dump(mode="json") for item in activities])
    assert activities[-1].data["is_error"] is False
    command = captured["command"]
    assert isinstance(command, list)
    assert _option_value(command, "--output-format") == "stream-json"
    assert "--verbose" in command
    assert "--include-hook-events" in command
    assert captured["stdin"] == subprocess.PIPE
    prompt_path = captured["prompt_path"]
    assert isinstance(prompt_path, Path)
    assert not prompt_path.exists()


def test_observed_runtime_supports_incident_structured_contract(tmp_path: Path) -> None:
    stream = [
        {"type": "system", "subtype": "init", "model": "deepseek-test"},
        {
            "type": "result",
            "subtype": "success",
            "session_id": "runtime-incident-stream",
            "structured_output": {
                "status": "needs_input",
                "message": "Need the page title.",
                "question": "What title is visible on the page?",
            },
            "usage": {"input_tokens": 2, "output_tokens": 3},
        },
    ]
    script = "import sys\n" + "\n".join(
        f"print({json.dumps(json.dumps(item))}, flush=True)" for item in stream
    )

    def popen_factory(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
        return subprocess.Popen([sys.executable, "-c", script], **kwargs)

    runtime = ClaudeCodeRuntime(_settings(tmp_path), popen_factory=popen_factory)
    activities = []

    result = runtime.run_structured_observed(
        _turn(tmp_path),
        IncidentDecision,
        "incident-run-1",
        activities.append,
    )

    assert result.runtime_session_id == "runtime-incident-stream"
    assert result.output.status == IncidentStatus.NEEDS_INPUT
    assert result.output.question == "What title is visible on the page?"
    assert activities[0].kind == RuntimeEventKind.SYSTEM_INIT


def test_observed_runtime_blocks_repository_wide_glob(tmp_path: Path) -> None:
    stream = [
        {"type": "system", "subtype": "init", "model": "deepseek-test"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-broad-glob",
                        "name": "Glob",
                        "input": {"pattern": "**/*"},
                    }
                ]
            },
        },
    ]
    script = "import sys, time\n" + "\n".join(
        f"print({json.dumps(json.dumps(item))}, flush=True)" for item in stream
    ) + "\ntime.sleep(30)"

    def popen_factory(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
        return subprocess.Popen([sys.executable, "-c", script], **kwargs)

    runtime = ClaudeCodeRuntime(_settings(tmp_path), popen_factory=popen_factory)
    activities = []

    with pytest.raises(ClaudeCodeError, match="范围过大的源码搜索"):
        runtime.run_observed(_turn(tmp_path), "run-broad-glob", activities.append)

    assert activities[-1].kind == RuntimeEventKind.POLICY_BLOCKED
    assert activities[-1].tool_name == "Glob"
    assert activities[-1].data["pattern"] == "**/*"
    assert "禁止通配整个项目" in activities[-1].data["reason"]


def test_observed_runtime_can_be_interrupted(tmp_path: Path) -> None:
    started = threading.Event()

    def popen_factory(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; print('{}', flush=True); time.sleep(30)"],
            **kwargs,
        )
        started.set()
        return process

    runtime = ClaudeCodeRuntime(_settings(tmp_path), popen_factory=popen_factory)
    errors: list[Exception] = []

    def invoke() -> None:
        try:
            runtime.run_observed(_turn(tmp_path), "run-interrupt", lambda _event: None)
        except Exception as exc:  # noqa: BLE001 - test captures worker failure
            errors.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert started.wait(timeout=2)
    for _ in range(100):
        if runtime.interrupt("run-interrupt"):
            break
        time.sleep(0.01)
    else:
        pytest.fail("The observed Runtime never registered its active process.")
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors and "interrupted" in str(errors[0]).casefold()
