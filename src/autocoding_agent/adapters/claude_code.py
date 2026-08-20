"""Claude Code CLI runtime with structured output and exact session resume."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision, AgentUsage, RuntimeResult, RuntimeTurn


class ClaudeCodeError(RuntimeError):
    """A recoverable, user-facing runtime failure."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class ClaudeCodeRuntime:
    """Run one turn through Claude Code without reimplementing its agent loop."""

    def __init__(self, settings: Settings, runner: Runner = subprocess.run) -> None:
        self.settings = settings
        self._runner = runner

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        command = self.build_command(turn)
        try:
            completed = self._runner(
                command,
                cwd=turn.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.settings.claude_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ClaudeCodeError(
                "Claude Code executable was not found. Set AUTO_CODING_CLAUDE_COMMAND "
                "to the real claude.exe path."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError(
                f"Claude Code exceeded the {self.settings.claude_timeout_seconds}s turn timeout."
            ) from exc

        if completed.returncode != 0:
            detail = _redact((completed.stderr or completed.stdout).strip())
            raise ClaudeCodeError(detail or "Claude Code returned a non-zero exit code.")

        try:
            envelope: dict[str, Any] = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeCodeError(
                "Claude Code did not return a valid JSON result envelope."
            ) from exc

        if envelope.get("is_error") is True or envelope.get("subtype") not in {
            None,
            "success",
        }:
            detail = _redact(str(envelope.get("result") or "Claude Code reported an error."))
            raise ClaudeCodeError(detail)

        structured = envelope.get("structured_output")
        if structured is None:
            raise ClaudeCodeError(
                "Claude Code completed without the structured result required by the "
                "agent contract."
            )
        try:
            decision = AgentDecision.model_validate(structured)
        except ValidationError as exc:
            raise ClaudeCodeError(f"Claude Code returned an invalid agent decision: {exc}") from exc

        runtime_session_id = envelope.get("session_id")
        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            raise ClaudeCodeError("Claude Code result did not include a resumable session id.")

        usage_data = envelope.get("usage") or {}
        usage = AgentUsage(
            input_tokens=int(usage_data.get("input_tokens", 0) or 0),
            output_tokens=int(usage_data.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage_data.get("cache_read_input_tokens", 0) or 0),
            cost_usd=_optional_float(envelope.get("total_cost_usd")),
            duration_ms=_optional_int(envelope.get("duration_ms")),
            turns=_optional_int(envelope.get("num_turns")),
        )
        return RuntimeResult(
            decision=decision,
            runtime_session_id=runtime_session_id,
            usage=usage,
        )

    def build_command(self, turn: RuntimeTurn) -> list[str]:
        schema = json.dumps(AgentDecision.model_json_schema(), ensure_ascii=False)
        command = [
            self.settings.claude_command,
            "-p",
            "--bare",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--output-format",
            "json",
            "--model",
            self.settings.claude_model,
            "--permission-mode",
            turn.permission_mode,
            "--tools",
            ",".join(turn.tools),
            "--allowedTools",
            *turn.allowed_tools,
            "--append-system-prompt",
            turn.system_prompt,
            "--json-schema",
            schema,
        ]
        if turn.capability_dir:
            command.extend(["--add-dir", turn.capability_dir])
        if self.settings.max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(self.settings.max_budget_usd)])
        if turn.runtime_session_id:
            command.extend(["--resume", turn.runtime_session_id])
        else:
            command.extend(["--session-id", turn.session_id])
        command.append(turn.user_message)
        return command


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _redact(text: str) -> str:
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|auth(?:orization)?|token|password|secret)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    return re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", text)
