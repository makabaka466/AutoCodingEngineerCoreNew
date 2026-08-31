"""Claude Code CLI runtime with structured output and exact session resume."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from autocoding_agent.adapters.process_options import hidden_window_options
from autocoding_agent.config import Settings, resolve_claude_command
from autocoding_agent.core.models import AgentDecision, AgentUsage, RuntimeResult, RuntimeTurn
from autocoding_agent.core.runtime.models import RuntimeActivity, RuntimeEventKind
from autocoding_agent.ports.runtime import RuntimeEventSink, RuntimeInterruptedError
from autocoding_agent.ports.structured_runtime import StructuredRuntimeResult


class ClaudeCodeError(RuntimeError):
    """A recoverable, user-facing runtime failure."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
PopenFactory = Callable[..., subprocess.Popen[str]]
StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)
logger = logging.getLogger("autocoding_agent.runtime.claude_code")
_WINDOWS_COMMAND_LINE_LIMIT = 32_767


class ClaudeCodeRuntime:
    """Run one turn through Claude Code without reimplementing its agent loop."""

    def __init__(
        self,
        settings: Settings,
        runner: Runner = subprocess.run,
        popen_factory: PopenFactory = subprocess.Popen,
    ) -> None:
        self.settings = settings
        self._runner = runner
        self._popen_factory = popen_factory
        self._uses_default_runner = runner is subprocess.run
        self._uses_default_popen = popen_factory is subprocess.Popen
        self._active: dict[str, subprocess.Popen[str]] = {}
        self._interrupted: set[str] = set()
        self._active_lock = threading.Lock()

    def run(self, turn: RuntimeTurn) -> RuntimeResult:
        result = self.run_structured(turn, AgentDecision)
        return RuntimeResult(
            decision=result.output,
            runtime_session_id=result.runtime_session_id,
            usage=result.usage,
        )

    def run_observed(
        self,
        turn: RuntimeTurn,
        run_id: str,
        event_sink: RuntimeEventSink,
    ) -> RuntimeResult:
        """Run stream-json and emit sanitized lifecycle evidence as it arrives."""

        result = self.run_structured_observed(turn, AgentDecision, run_id, event_sink)
        return RuntimeResult(
            decision=result.output,
            runtime_session_id=result.runtime_session_id,
            usage=result.usage,
        )

    def run_structured_observed(
        self,
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
        run_id: str,
        event_sink: RuntimeEventSink,
    ) -> StructuredRuntimeResult[StructuredOutputT]:
        """Run any structured contract through stream-json with live activity events."""

        with self._launch_invocation(
            turn,
            response_model,
            stream=True,
            validate=self._uses_default_popen,
        ) as command:
            return self._execute_structured_observed(
                command,
                turn,
                response_model,
                run_id,
                event_sink,
            )

    def _execute_structured_observed(
        self,
        command: list[str],
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
        run_id: str,
        event_sink: RuntimeEventSink,
    ) -> StructuredRuntimeResult[StructuredOutputT]:
        """Execute one prepared streaming invocation while its prompt file remains alive."""

        started_at = time.monotonic()
        logger.info(
            "observed_turn_started session_id=%s run_id=%s mode=%s model=%s resumed=%s "
            "command=%s workspace=%s",
            turn.session_id,
            run_id,
            turn.mode.value,
            self.settings.claude_model,
            bool(turn.runtime_session_id),
            command[0],
            turn.workspace,
        )
        try:
            process = self._popen_factory(
                command,
                cwd=turn.workspace,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_window_options(),
            )
        except FileNotFoundError as exc:
            logger.error(
                "observed_turn_failed session_id=%s run_id=%s "
                "reason=runtime_launch_target_not_found command=%s workspace=%s",
                turn.session_id,
                run_id,
                command[0],
                turn.workspace,
            )
            raise self._launch_not_found_error(command[0], turn.workspace, exc) from exc
        except OSError as exc:
            detail = _log_safe_detail(_redact(str(exc)))
            logger.error(
                "observed_turn_failed session_id=%s run_id=%s reason=runtime_os_error "
                "command=%s workspace=%s detail=%s",
                turn.session_id,
                run_id,
                command[0],
                turn.workspace,
                detail,
            )
            raise ClaudeCodeError(f"Claude Code 进程启动失败：{detail}") from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            self._terminate_process(process)
            raise ClaudeCodeError("Claude Code streaming pipes were not created.")
        with self._active_lock:
            self._active[run_id] = process
            self._interrupted.discard(run_id)

        line_queue: Queue[str | None] = Queue()
        stderr_chunks: list[str] = []

        def write_stdin() -> None:
            assert process.stdin is not None
            try:
                process.stdin.write(turn.user_message)
            except (BrokenPipeError, OSError):
                # The process error/result envelope remains the authoritative failure signal.
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                line_queue.put(line)
            line_queue.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            stderr_chunks.append(process.stderr.read())

        stdin_thread = threading.Thread(target=write_stdin, daemon=True)
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdin_thread.start()
        stdout_thread.start()
        stderr_thread.start()
        deadline = started_at + self.settings.claude_timeout_seconds
        last_heartbeat = started_at
        stdout_done = False
        result_envelope: dict[str, Any] | None = None
        tool_context: dict[str, tuple[str, dict[str, Any]]] = {}
        try:
            while not (stdout_done and process.poll() is not None):
                now = time.monotonic()
                if now >= deadline:
                    self._terminate_process(process)
                    raise ClaudeCodeError(
                        f"Claude Code exceeded the {self.settings.claude_timeout_seconds}s "
                        "turn timeout."
                    )
                try:
                    line = line_queue.get(timeout=min(0.25, max(0.01, deadline - now)))
                except Empty:
                    if now - last_heartbeat >= 5:
                        event_sink(
                            RuntimeActivity(
                                run_id=run_id,
                                kind=RuntimeEventKind.HEARTBEAT,
                                summary="Claude Code process is still running.",
                            )
                        )
                        last_heartbeat = now
                    continue
                if line is None:
                    stdout_done = True
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    event_sink(
                        RuntimeActivity(
                            run_id=run_id,
                            kind=RuntimeEventKind.PROTOCOL_WARNING,
                            summary="Ignored one invalid stream-json line.",
                        )
                    )
                    continue
                if not isinstance(envelope, dict):
                    continue
                if envelope.get("type") == "result":
                    result_envelope = envelope
                for activity in _stream_activities(
                    envelope,
                    run_id=run_id,
                    workspace=turn.workspace,
                    tool_context=tool_context,
                ):
                    event_sink(activity)

            returncode = process.wait(timeout=5)
            stderr_thread.join(timeout=1)
            interrupted = self._was_interrupted(run_id)
            if interrupted:
                raise RuntimeInterruptedError("Claude Code run was interrupted by the host.")
            if returncode != 0:
                detail = _redact("".join(stderr_chunks).strip())
                raise ClaudeCodeError(detail or "Claude Code returned a non-zero exit code.")
            if result_envelope is None:
                raise ClaudeCodeError("Claude Code stream ended without a result envelope.")
            result = _structured_result_from_envelope(result_envelope, response_model)
            logger.info(
                "observed_turn_completed session_id=%s run_id=%s runtime_session_id=%s "
                "elapsed_ms=%d",
                turn.session_id,
                run_id,
                result.runtime_session_id,
                _elapsed_ms(started_at),
            )
            return result
        finally:
            if process.poll() is None:
                self._terminate_process(process)
            stdin_thread.join(timeout=1)
            with self._active_lock:
                self._active.pop(run_id, None)
                self._interrupted.discard(run_id)

    def interrupt(self, run_id: str) -> bool:
        with self._active_lock:
            process = self._active.get(run_id)
            if process is None or process.poll() is not None:
                return False
            self._interrupted.add(run_id)
        self._terminate_process(process)
        return True

    def run_structured(
        self,
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
    ) -> StructuredRuntimeResult[StructuredOutputT]:
        """Run Claude Code with any project-owned Pydantic output contract."""

        with self._launch_invocation(
            turn,
            response_model,
            validate=self._uses_default_runner,
        ) as command:
            return self._execute_structured(command, turn, response_model)

    def _execute_structured(
        self,
        command: list[str],
        turn: RuntimeTurn,
        response_model: type[StructuredOutputT],
    ) -> StructuredRuntimeResult[StructuredOutputT]:
        """Execute one prepared JSON invocation while its prompt file remains alive."""

        started_at = time.monotonic()
        logger.info(
            "turn_started session_id=%s mode=%s model=%s resumed=%s command=%s workspace=%s",
            turn.session_id,
            turn.mode.value,
            self.settings.claude_model,
            bool(turn.runtime_session_id),
            command[0],
            turn.workspace,
        )
        try:
            completed = self._runner(
                command,
                cwd=turn.workspace,
                input=turn.user_message,
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
                "turn_failed session_id=%s reason=runtime_launch_target_not_found "
                "command=%s workspace=%s elapsed_ms=%d",
                turn.session_id,
                command[0],
                turn.workspace,
                _elapsed_ms(started_at),
            )
            raise self._launch_not_found_error(command[0], turn.workspace, exc) from exc
        except OSError as exc:
            detail = _log_safe_detail(_redact(str(exc)))
            logger.error(
                "turn_failed session_id=%s reason=runtime_os_error command=%s workspace=%s "
                "elapsed_ms=%d detail=%s",
                turn.session_id,
                command[0],
                turn.workspace,
                _elapsed_ms(started_at),
                detail,
            )
            raise ClaudeCodeError(f"Claude Code 进程启动失败：{detail}") from exc
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
            raise ClaudeCodeError(f"Claude Code returned an invalid {label}: {exc}") from exc

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
        *,
        system_prompt_file: str | Path,
        stream: bool = False,
    ) -> list[str]:
        prompt_path = Path(system_prompt_file)
        if not prompt_path.is_file():
            raise ValueError(f"System prompt file does not exist: {prompt_path}")
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
            "stream-json" if stream else "json",
            "--input-format",
            "text",
            "--model",
            self.settings.claude_model,
            "--permission-mode",
            turn.permission_mode,
            "--tools",
            ",".join(turn.tools),
            "--allowedTools",
            *turn.allowed_tools,
            "--append-system-prompt-file",
            str(prompt_path),
            "--json-schema",
            schema,
        ]
        if stream:
            command.extend(["--verbose", "--include-hook-events"])
        read_directories = [
            item
            for item in [turn.capability_dir, *turn.additional_dirs]
            if item
        ]
        for directory in dict.fromkeys(read_directories):
            command.extend(["--add-dir", directory])
        if self.settings.max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(self.settings.max_budget_usd)])
        if turn.runtime_session_id:
            command.extend(["--resume", turn.runtime_session_id])
        else:
            command.extend(["--session-id", turn.session_id])
        return command

    @contextmanager
    def _launch_invocation(
        self,
        turn: RuntimeTurn,
        response_model: type[BaseModel],
        *,
        stream: bool = False,
        validate: bool,
    ) -> Iterator[list[str]]:
        """Keep large prompts out of argv and remove the temporary file after the run."""

        with TemporaryDirectory(prefix="ace-claude-") as prompt_dir:
            prompt_path = Path(prompt_dir) / "system-prompt.md"
            prompt_path.write_text(turn.system_prompt, encoding="utf-8")
            command = self.build_command(
                turn,
                response_model,
                system_prompt_file=prompt_path,
                stream=stream,
            )
            command = self._prepare_launch_command(command, turn, validate=validate)
            command_chars = _command_line_chars(command)
            schema_chars = len(command[command.index("--json-schema") + 1])
            logger.info(
                "runtime_input_prepared session_id=%s transport=prompt_file+stdin "
                "command_chars=%d system_prompt_chars=%d user_message_chars=%d "
                "json_schema_chars=%d",
                turn.session_id,
                command_chars,
                len(turn.system_prompt),
                len(turn.user_message),
                schema_chars,
            )
            _validate_command_line_length(command, command_chars)
            yield command

    def _prepare_launch_command(
        self,
        command: list[str],
        turn: RuntimeTurn,
        *,
        validate: bool,
    ) -> list[str]:
        """Recover stale executable settings and validate real process launch targets."""

        if not validate:
            return command
        configured = command[0]
        resolved = resolve_claude_command(configured)
        if resolved != configured:
            logger.warning(
                "runtime_command_recovered configured=%s resolved=%s",
                configured,
                resolved,
            )
        prepared = [resolved, *command[1:]]
        workspace = Path(turn.workspace).expanduser()
        if not workspace.is_dir():
            error = ClaudeCodeError(
                f"项目目录不存在或不可访问：{workspace}。请在系统配置中重新选择项目目录。"
            )
            logger.error(
                "runtime_launch_preflight_failed reason=workspace_not_found command=%s "
                "workspace=%s",
                resolved,
                workspace,
            )
            raise error
        executable = Path(resolved).expanduser()
        if not executable.is_file():
            error = ClaudeCodeError(
                f"Claude Code 程序不存在或不可访问：{resolved}。"
                "请在系统配置中重新选择真实的 claude.exe。"
            )
            logger.error(
                "runtime_launch_preflight_failed reason=runtime_not_found command=%s "
                "workspace=%s",
                resolved,
                workspace,
            )
            raise error
        return prepared

    @staticmethod
    def _launch_not_found_error(
        command: str,
        workspace: str,
        cause: FileNotFoundError,
    ) -> ClaudeCodeError:
        """Differentiate an invalid executable from an invalid subprocess working directory."""

        workspace_path = Path(workspace).expanduser()
        if not workspace_path.is_dir():
            return ClaudeCodeError(
                f"项目目录不存在或不可访问：{workspace_path}。"
                "请在系统配置中重新选择项目目录。"
            )
        executable = Path(command).expanduser()
        if not executable.is_file():
            return ClaudeCodeError(
                f"Claude Code 程序不存在或不可访问：{command}。"
                "请在系统配置中重新选择真实的 claude.exe。"
            )
        detail = " ".join(str(cause).split())[:500]
        return ClaudeCodeError(
            "Claude Code 进程启动失败，但程序和项目目录均存在。"
            f"系统错误：{detail or 'FileNotFoundError'}"
        )

    def _was_interrupted(self, run_id: str) -> bool:
        with self._active_lock:
            return run_id in self._interrupted

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _runtime_result_from_envelope(envelope: dict[str, Any]) -> RuntimeResult:
    result = _structured_result_from_envelope(envelope, AgentDecision)
    return RuntimeResult(
        decision=result.output,
        runtime_session_id=result.runtime_session_id,
        usage=result.usage,
    )


def _structured_result_from_envelope(
    envelope: dict[str, Any],
    response_model: type[StructuredOutputT],
) -> StructuredRuntimeResult[StructuredOutputT]:
    if envelope.get("is_error") is True or envelope.get("subtype") not in {
        None,
        "success",
    }:
        raise ClaudeCodeError(
            _redact(str(envelope.get("result") or "Claude Code reported an error."))
        )
    structured = envelope.get("structured_output")
    if structured is None:
        raise ClaudeCodeError(
            "Claude Code completed without the structured result required by the agent contract."
        )
    try:
        output = response_model.model_validate(structured)
    except ValidationError as exc:
        label = "agent decision" if response_model is AgentDecision else "structured decision"
        raise ClaudeCodeError(f"Claude Code returned an invalid {label}: {exc}") from exc
    runtime_session_id = envelope.get("session_id")
    if not isinstance(runtime_session_id, str) or not runtime_session_id:
        raise ClaudeCodeError("Claude Code result did not include a resumable session id.")
    usage_data = envelope.get("usage") or {}
    return StructuredRuntimeResult(
        output=output,
        runtime_session_id=runtime_session_id,
        usage=AgentUsage(
            input_tokens=int(usage_data.get("input_tokens", 0) or 0),
            output_tokens=int(usage_data.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage_data.get("cache_read_input_tokens", 0) or 0),
            cost_usd=_optional_float(envelope.get("total_cost_usd")),
            duration_ms=_optional_int(envelope.get("duration_ms")),
            turns=_optional_int(envelope.get("num_turns")),
        ),
    )


def _stream_activities(
    envelope: dict[str, Any],
    *,
    run_id: str,
    workspace: str,
    tool_context: dict[str, tuple[str, dict[str, Any]]],
) -> list[RuntimeActivity]:
    event_type = str(envelope.get("type") or "")
    activities: list[RuntimeActivity] = []
    if event_type == "system" and envelope.get("subtype") == "init":
        activities.append(
            RuntimeActivity(
                run_id=run_id,
                kind=RuntimeEventKind.SYSTEM_INIT,
                summary="Claude Code initialized the Runtime session.",
                data={
                    "model": _safe_runtime_text(str(envelope.get("model") or ""), workspace),
                },
            )
        )
        return activities

    message = envelope.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return activities
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if event_type == "assistant" and block_type == "text":
            summary = _safe_runtime_text(str(block.get("text") or ""), workspace, limit=500)
            if summary:
                activities.append(
                    RuntimeActivity(
                        run_id=run_id,
                        kind=RuntimeEventKind.ASSISTANT_MESSAGE,
                        summary=summary,
                    )
                )
        elif event_type == "assistant" and block_type == "tool_use":
            tool_name = str(block.get("name") or "unknown")
            tool_use_id = str(block.get("id") or "") or None
            data = _safe_tool_data(tool_name, block.get("input"), workspace)
            if tool_use_id:
                tool_context[tool_use_id] = (tool_name, data)
            activities.append(
                RuntimeActivity(
                    run_id=run_id,
                    kind=RuntimeEventKind.TOOL_STARTED,
                    summary=f"Claude Code started {tool_name}.",
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    data=data,
                )
            )
        elif event_type == "user" and block_type == "tool_result":
            tool_use_id = str(block.get("tool_use_id") or "") or None
            tool_name, data = tool_context.get(tool_use_id or "", ("unknown", {}))
            is_error = bool(block.get("is_error", False))
            activities.append(
                RuntimeActivity(
                    run_id=run_id,
                    kind=RuntimeEventKind.TOOL_FINISHED,
                    summary=(
                        f"Claude Code {tool_name} failed."
                        if is_error
                        else f"Claude Code finished {tool_name}."
                    ),
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    data={**data, "is_error": is_error},
                )
            )
    return activities


def _safe_tool_data(tool_name: str, raw_input: Any, workspace: str) -> dict[str, Any]:
    if not isinstance(raw_input, dict):
        return {}
    if tool_name.casefold() == "bash":
        return {
            "command": _safe_runtime_text(str(raw_input.get("command") or ""), workspace, limit=800)
        }
    for key in ("file_path", "path"):
        if raw_input.get(key):
            return {"path": _safe_path_hint(str(raw_input[key]), workspace)}
    if raw_input.get("pattern"):
        return {"pattern": _safe_runtime_text(str(raw_input["pattern"]), workspace, limit=300)}
    return {}


def _safe_path_hint(value: str, workspace: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(Path(workspace).resolve()).as_posix()
        except ValueError:
            return "<OUTSIDE_WORKSPACE>"
    if ".." in candidate.parts:
        return "<OUTSIDE_WORKSPACE>"
    return candidate.as_posix()


def _safe_runtime_text(value: str, workspace: str, *, limit: int = 300) -> str:
    text = _redact(value)
    text = re.sub(re.escape(str(Path(workspace).resolve())), "<WORKSPACE>", text, flags=re.I)
    text = re.sub(re.escape(str(Path.home())), "<USER_HOME>", text, flags=re.I)
    return " ".join(text.split())[:limit]


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _command_line_chars(command: list[str]) -> int:
    """Return the Windows CreateProcess command-line size without exposing its content."""

    return len(subprocess.list2cmdline(command))


def _validate_command_line_length(command: list[str], command_chars: int | None = None) -> None:
    """Fail before CreateProcess when the remaining argv still exceeds Windows limits."""

    if os.name != "nt":
        return
    measured = command_chars if command_chars is not None else _command_line_chars(command)
    if measured < _WINDOWS_COMMAND_LINE_LIMIT:
        return
    logger.error(
        "runtime_launch_preflight_failed reason=command_line_too_long command_chars=%d "
        "limit=%d",
        measured,
        _WINDOWS_COMMAND_LINE_LIMIT,
    )
    raise ClaudeCodeError(
        "Claude Code 启动参数仍超过 Windows 命令行长度限制。"
        "请减少挂载目录或升级 Runtime 传输配置。"
    )


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
