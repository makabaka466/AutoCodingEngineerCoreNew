"""Claude Code CLI runtime with structured output and exact session resume."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from autocoding_agent.adapters.process_options import hidden_window_options
from autocoding_agent.config import Settings
from autocoding_agent.core.models import AgentDecision, AgentUsage, RuntimeResult, RuntimeTurn
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class ClaudeCodeError(RuntimeError):
    """A recoverable, user-facing runtime failure."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)
logger = logging.getLogger("autocoding_agent.runtime.claude_code")


class ClaudeCodeRuntime:
    """Run one turn through Claude Code without reimplementing its agent loop."""

    def __init__(self, settings: Settings, runner: Runner = subprocess.run) -> None:
        self.settings = settings
        self._runner = runner

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        result = self.run_structured(turn, AgentDecision)
        return RuntimeResult(
            decision=result.output,
            runtime_session_id=result.runtime_session_id,
            usage=result.usage,
        )

    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
    ) -> StructuredRuntimeResult[StructuredOutputT]:
        """Run Claude Code with any project-owned Pydantic output contract."""

        command = self.build_command(turn, response_model)
        started_at = time.monotonic()
        logger.info(
            "turn_started session_id=%s mode=%s model=%s resumed=%s workspace=%s",
            turn.session_id,
            turn.mode.value,
            self.settings.claude_model,
            bool(turn.runtime_session_id),
            turn.workspace,
        )
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
                **hidden_window_options(),
            )
        except FileNotFoundError as exc:
            logger.error(
                "turn_failed session_id=%s reason=runtime_not_found elapsed_ms=%d",
                turn.session_id,
                _elapsed_ms(started_at),
            )
            raise ClaudeCodeError(
                "Claude Code executable was not found. Set AUTO_CODING_CLAUDE_COMMAND "
                "to the real claude.exe path."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            logger.warning(
                "turn_timeout session_id=%s mode=%s timeout_seconds=%d elapsed_ms=%d",
                turn.session_id,
                turn.mode.value,
                self.settings.claude_timeout_seconds,
                _elapsed_ms(started_at),
            )
            raise ClaudeCodeError(
                f"Claude Code exceeded the {self.settings.claude_timeout_seconds}s turn timeout."
            ) from exc

        if completed.returncode != 0:
            detail = _redact((completed.stderr or completed.stdout).strip())
            logger.error(
                "turn_failed session_id=%s reason=nonzero_exit returncode=%d elapsed_ms=%d "
                "detail=%s",
                turn.session_id,
                completed.returncode,
                _elapsed_ms(started_at),
                _log_safe_detail(detail),
            )
            raise ClaudeCodeError(detail or "Claude Code returned a non-zero exit code.")

        try:
            envelope: dict[str, Any] = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            logger.error(
                "turn_failed session_id=%s reason=invalid_json elapsed_ms=%d",
                turn.session_id,
                _elapsed_ms(started_at),
            )
            raise ClaudeCodeError(
                "Claude Code did not return a valid JSON result envelope."
            ) from exc

        if envelope.get("is_error") is True or envelope.get("subtype") not in {
            None,
            "success",
        }:
            detail = _redact(str(envelope.get("result") or "Claude Code reported an error."))
            logger.error(
                "turn_failed session_id=%s reason=model_error subtype=%s elapsed_ms=%d detail=%s",
                turn.session_id,
                envelope.get("subtype"),
                _elapsed_ms(started_at),
                _log_safe_detail(detail),
            )
            raise ClaudeCodeError(detail)

        structured = envelope.get("structured_output")
        if structured is None:
            logger.error(
                "turn_failed session_id=%s reason=missing_structured_output elapsed_ms=%d",
                turn.session_id,
                _elapsed_ms(started_at),
            )
            raise ClaudeCodeError(
                "Claude Code completed without the structured result required by the "
                "agent contract."
            )
        try:
            output = response_model.model_validate(structured)
        except ValidationError as exc:
            label = "agent decision" if response_model is AgentDecision else "structured decision"
            logger.error(
                "turn_failed session_id=%s reason=invalid_%s elapsed_ms=%d",
                turn.session_id,
                label.replace(" ", "_"),
                _elapsed_ms(started_at),
            )
            raise ClaudeCodeError(
                f"Claude Code returned an invalid {label}: {exc}"
            ) from exc

        runtime_session_id = envelope.get("session_id")
        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            logger.error(
                "turn_failed session_id=%s reason=missing_runtime_session elapsed_ms=%d",
                turn.session_id,
                _elapsed_ms(started_at),
            )
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
        logger.info(
            "turn_completed session_id=%s runtime_session_id=%s status=%s elapsed_ms=%d "
            "input_tokens=%d output_tokens=%d",
            turn.session_id,
            runtime_session_id,
            getattr(output, "status", "unknown"),
            _elapsed_ms(started_at),
            usage.input_tokens,
            usage.output_tokens,
        )
        return StructuredRuntimeResult(
            output=output,
            runtime_session_id=runtime_session_id,
            usage=usage,
        )

    def build_command(
        self,
        turn: RuntimeTurn,
        response_model: type[BaseModel] = AgentDecision,
    ) -> list[str]:
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
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


def _elapsed_ms(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


def _log_safe_detail(detail: str, limit: int = 1000) -> str:
    """Keep provider diagnostics useful without allowing multiline log injection."""

    compact = " ".join(_redact(detail).split())
    return compact[:limit] or "none"


def _redact(text: str) -> str:
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|auth(?:orization)?|token|password|secret)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    return re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", text)
