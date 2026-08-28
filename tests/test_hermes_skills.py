from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autocoding_agent.adapters.hermes_skills import (
    HermesCliSkillService,
    HermesSkillError,
)
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
