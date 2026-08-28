from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autocoding_agent.adapters.hermes_skills import (
    HermesCliSkillService,
    HermesSkillError,
    build_configured_hermes_service,
)
from autocoding_agent.config import Settings
from autocoding_agent.core.hermes import HermesSkillRequest


def _installed_skill(home: Path, category: str, name: str, description: str) -> None:
    skill_dir = home / "skills" / category / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# Skill\n",
        encoding="utf-8",
    )


def _service(
    tmp_path: Path,
    runner,
    *,
    max_output_chars: int = 12000,
    inherited_endpoint: str | None = None,
    inherited_model: str | None = None,
    credential_reader=None,
) -> HermesCliSkillService:
    home = tmp_path / "hermes-home"
    command = tmp_path / "hermes.exe"
    command.write_bytes(b"test executable placeholder")
    _installed_skill(home, "software-development", "debug-method", "Trace causal evidence.")
    _installed_skill(home, "research", "source-check", "Compare primary sources.")
    _installed_skill(home, "creative", "poster", "Design a poster.")
    return HermesCliSkillService(
        command=str(command),
        home=home,
        allowed_categories=["software-development", "research"],
        max_output_chars=max_output_chars,
        runner=runner,
        inherited_endpoint=inherited_endpoint,
        inherited_model=inherited_model,
        credential_reader=credential_reader,
    )


def test_discovers_only_allowed_nested_skills(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "ok", ""),
    )

    assert [(item.category, item.name) for item in service.available_skills()] == [
        ("software-development", "debug-method"),
        ("research", "source-check"),
    ]


def test_invokes_exact_skill_with_isolated_rules_and_redacts_boundary_text(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def runner(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args,
            0,
            "Prefer a bounded patch. api_key=provider-secret",
            "",
        )

    service = _service(tmp_path, runner)
    result = service.invoke(
        HermesSkillRequest(
            skill="debug-method",
            question="How should retries be diagnosed? token=user-secret",
            reason="Need a reusable failure-analysis method.",
        )
    )

    args = captured["args"]
    kwargs = captured["kwargs"]
    assert args[0] == service.command
    assert args[1:] == [
        "chat",
        "--query-file",
        "-",
        "--toolsets",
        "web",
        "--skills",
        "debug-method",
        "--max-turns",
        "4",
        "--quiet",
        "--ignore-rules",
        "--source",
        "tool",
    ]
    assert kwargs["cwd"] == str(service.home)
    assert kwargs["env"]["HERMES_HOME"] == str(service.home)
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert "user-secret" not in kwargs["input"]
    assert "provider-secret" not in result.output
    assert result.input_redacted is True
    assert result.output_redacted is True


def test_rejects_unknown_skill_before_starting_process(tmp_path: Path) -> None:
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    service = _service(tmp_path, runner)

    with pytest.raises(HermesSkillError, match="allowlist"):
        service.invoke(
            HermesSkillRequest(
                skill="../debug-method",
                question="How should this be analyzed?",
                reason="Need guidance.",
            )
        )

    assert called is False


def test_timeout_is_sanitized_and_reported(tmp_path: Path) -> None:
    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    service = _service(tmp_path, runner)

    with pytest.raises(HermesSkillError, match="timed out after 120 seconds"):
        service.invoke(
            HermesSkillRequest(
                skill="debug-method",
                question="How should this be analyzed?",
                reason="Need guidance.",
            )
        )


def test_output_is_bounded(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "x" * 2000, ""),
        max_output_chars=1000,
    )

    result = service.invoke(
        HermesSkillRequest(
            skill="debug-method",
            question="How should this be analyzed?",
            reason="Need guidance.",
        )
    )

    assert result.output_truncated is True
    assert result.output.endswith("[TRUNCATED]")
    assert len(result.output) <= 1000


def test_inherits_ace_deepseek_route_only_inside_hermes_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "parent-token-must-be-replaced")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-anthropic-key")

    def runner(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "Use a bounded diagnostic plan.", "")

    service = _service(
        tmp_path,
        runner,
        inherited_endpoint="https://api.deepseek.com/anthropic/",
        inherited_model="deepseek-v4-flash",
        credential_reader=lambda: "ace-deepseek-secret",
    )
    service.invoke(
        HermesSkillRequest(
            skill="debug-method",
            question="How should this be diagnosed?",
            reason="Need a reusable investigation method.",
        )
    )

    args = captured["args"]
    kwargs = captured["kwargs"]
    assert args[-4:] == ["--model", "deepseek-v4-flash", "--provider", "custom"]
    assert kwargs["env"]["CUSTOM_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert kwargs["env"]["DEEPSEEK_API_KEY"] == "ace-deepseek-secret"
    assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "ace-deepseek-secret" not in " ".join(args)
    assert "ace-deepseek-secret" not in kwargs["input"]


def test_inherited_route_fails_before_launch_when_ace_key_is_missing(tmp_path: Path) -> None:
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    service = _service(
        tmp_path,
        runner,
        inherited_endpoint="https://api.deepseek.com/anthropic",
        inherited_model="deepseek-v4-flash",
        credential_reader=lambda: None,
    )

    with pytest.raises(HermesSkillError, match="API key is unavailable"):
        service.invoke(
            HermesSkillRequest(
                skill="debug-method",
                question="How should this be diagnosed?",
                reason="Need guidance.",
            )
        )

    assert called is False


def test_configured_service_uses_ace_endpoint_and_independent_hermes_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "hermes-home"
    command = tmp_path / "hermes.exe"
    command.write_bytes(b"test executable placeholder")
    _installed_skill(home, "software-development", "debug-method", "Trace evidence.")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ace-secret")

    service = build_configured_hermes_service(
        Settings(
            _env_file=None,
            hermes_command=str(command),
            hermes_home=home,
            hermes_model="deepseek-v4-flash",
        )
    )

    assert isinstance(service, HermesCliSkillService)
    assert service.inherited_endpoint == "https://api.deepseek.com/anthropic"
    assert service.inherited_model == "deepseek-v4-flash"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.deepseek.com/anthropic",
        "https://api.deepseek.com.attacker.test/anthropic",
        "https://api.deepseek.com/v1",
    ],
)
def test_inherited_route_rejects_non_deepseek_anthropic_endpoint(
    tmp_path: Path,
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="DeepSeek /anthropic"):
        _service(
            tmp_path,
            lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "ok", ""),
            inherited_endpoint=endpoint,
            inherited_model="deepseek-v4-flash",
            credential_reader=lambda: "secret",
        )
