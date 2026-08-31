from __future__ import annotations

from pathlib import Path

import autocoding_agent.config as config_module


def test_command_resolution_uses_valid_user_value_after_stale_process_value(
    tmp_path: Path,
    monkeypatch,
) -> None:
    executable = tmp_path / "claude.exe"
    executable.touch()
    monkeypatch.setattr(
        config_module,
        "_windows_user_environment",
        lambda name: (
            str(executable) if name == "AUTO_CODING_CLAUDE_COMMAND" else None
        ),
    )
    monkeypatch.setattr(config_module.shutil, "which", lambda _name: None)
    monkeypatch.setenv("AUTO_CODING_CLAUDE_COMMAND", "C:/stale/claude.exe")
    monkeypatch.setenv("AUTO_TASK_AGENT_CLAUDE_CODE_COMMAND", "C:/legacy-stale/claude.exe")

    resolved = config_module.resolve_claude_command("C:/stale/claude.exe")

    assert resolved == str(executable.resolve())


def test_start_script_refreshes_saved_user_model_environment() -> None:
    script = (Path(__file__).parents[1] / "start.ps1").read_text(encoding="utf-8")

    assert 'GetEnvironmentVariable($name, "User")' in script
    assert 'SetEnvironmentVariable($name, $userValue, "Process")' in script
    assert 'IsNullOrWhiteSpace($currentValue)' not in script
