from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from autocoding_agent.model_setup import ClaudeModelSetupService, ModelSetupError


class FakeEnvironment:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.saved: list[dict[str, str]] = []

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set_many(self, values: Mapping[str, str]) -> None:
        saved = dict(values)
        self.saved.append(saved)
        self.values.update(saved)


def _claude_executable(tmp_path: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    executable = tmp_path / f"claude{suffix}"
    executable.touch()
    return executable


def _successful_runner(
    captured: dict[str, object] | None = None,
):
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if captured is not None:
            captured["command"] = command
            captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="2.1.228\n", stderr="")

    return run


def test_detect_validates_real_executable_and_uses_hidden_console_options(
    tmp_path: Path,
) -> None:
    executable = _claude_executable(tmp_path)
    captured: dict[str, object] = {}
    service = ClaudeModelSetupService(
        FakeEnvironment(), runner=_successful_runner(captured)
    )

    installation = service.detect(str(executable))

    assert installation.found is True
    assert installation.command == str(executable.resolve())
    assert installation.version == "2.1.228"
    assert captured["command"] == [str(executable.resolve()), "--version"]
    assert captured["timeout"] == 10
    if os.name == "nt":
        assert int(captured["creationflags"]) & subprocess.CREATE_NO_WINDOW


def test_inspect_requires_both_installation_and_api_key(tmp_path: Path) -> None:
    executable = _claude_executable(tmp_path)
    environment = FakeEnvironment(
        {
            "AUTO_CODING_CLAUDE_COMMAND": str(executable),
            "ANTHROPIC_BASE_URL": "https://provider.example/anthropic",
            "AUTO_CODING_CLAUDE_MODEL": "provider-model",
        }
    )
    service = ClaudeModelSetupService(environment, runner=_successful_runner())

    state = service.inspect()

    assert state.installation.found is True
    assert state.has_api_key is False
    assert state.ready is False
    assert not hasattr(state, "api_key")


def test_save_persists_provider_values_and_preserves_existing_blank_key(
    tmp_path: Path,
) -> None:
    executable = _claude_executable(tmp_path)
    environment = FakeEnvironment({"ANTHROPIC_AUTH_TOKEN": "existing-secret"})
    service = ClaudeModelSetupService(environment, runner=_successful_runner())

    state = service.save(
        command=str(executable),
        endpoint="https://provider.example/anthropic/",
        model=" model-v4 ",
        api_key="",
    )

    assert state.ready is True
    assert environment.values["AUTO_CODING_CLAUDE_COMMAND"] == str(executable.resolve())
    assert environment.values["ANTHROPIC_BASE_URL"] == "https://provider.example/anthropic"
    assert environment.values["AUTO_CODING_CLAUDE_MODEL"] == "model-v4"
    assert environment.values["ANTHROPIC_AUTH_TOKEN"] == "existing-secret"
    assert "ANTHROPIC_AUTH_TOKEN" not in environment.saved[-1]


def test_save_writes_new_key_without_returning_it(tmp_path: Path) -> None:
    executable = _claude_executable(tmp_path)
    environment = FakeEnvironment()
    service = ClaudeModelSetupService(environment, runner=_successful_runner())

    state = service.save(
        command=str(executable),
        endpoint="https://provider.example/anthropic",
        model="model-v4",
        api_key="new-secret",
    )

    assert environment.values["ANTHROPIC_AUTH_TOKEN"] == "new-secret"
    assert state.has_api_key is True
    assert "new-secret" not in repr(state)


@pytest.mark.parametrize(
    ("endpoint", "model", "api_key", "error"),
    [
        ("provider.example", "model-v4", "secret", "API 地址"),
        ("https://provider.example", " ", "secret", "模型名称"),
        ("https://provider.example", "model-v4", "", "API Key"),
    ],
)
def test_save_rejects_incomplete_provider_configuration(
    tmp_path: Path,
    endpoint: str,
    model: str,
    api_key: str,
    error: str,
) -> None:
    executable = _claude_executable(tmp_path)
    service = ClaudeModelSetupService(FakeEnvironment(), runner=_successful_runner())

    with pytest.raises(ModelSetupError, match=error):
        service.save(
            command=str(executable),
            endpoint=endpoint,
            model=model,
            api_key=api_key,
        )
