"""Claude Code discovery and model-provider configuration."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from autocoding_agent.adapters.process_options import hidden_window_options
from autocoding_agent.config import get_settings

logger = logging.getLogger("autocoding_agent.model_setup")

DEFAULT_ENDPOINT = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-v4-pro"


class ModelSetupError(ValueError):
    """A safe validation or persistence error suitable for the configuration UI."""


class EnvironmentStore(Protocol):
    """Small persistence boundary so secrets never need to enter project files."""

    def get(self, name: str) -> str | None: ...

    def set_many(self, values: Mapping[str, str]) -> None: ...


class UserEnvironmentStore:
    """Read process/user variables and persist configuration for the current user."""

    def get(self, name: str) -> str | None:
        process_value = os.environ.get(name)
        if process_value:
            return process_value
        if os.name != "nt":
            return None
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except (FileNotFoundError, OSError):
            return None
        return str(value) if value else None

    def set_many(self, values: Mapping[str, str]) -> None:
        cleaned = {name: value.strip() for name, value in values.items() if value.strip()}
        if os.name == "nt":
            try:
                import winreg

                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                    for name, value in cleaned.items():
                        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            except OSError as exc:
                raise ModelSetupError(f"无法保存 Windows 用户配置：{exc}") from exc
        for name, value in cleaned.items():
            os.environ[name] = value


@dataclass(frozen=True)
class ClaudeInstallation:
    """Validated Claude Code executable information."""

    found: bool
    command: str | None = None
    version: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ModelSetupState:
    """Secret-free state consumed by the desktop setup page."""

    installation: ClaudeInstallation
    endpoint: str
    model: str
    has_api_key: bool
    ready: bool


Runner = Callable[..., subprocess.CompletedProcess[str]]


class ClaudeModelSetupService:
    """Detect Claude Code and maintain the user's provider configuration."""

    def __init__(
        self,
        environment: EnvironmentStore | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.environment = environment or UserEnvironmentStore()
        self._runner = runner

    def inspect(self, command: str | None = None) -> ModelSetupState:
        """Return startup readiness without exposing the configured API key."""

        installation = self.detect(command)
        endpoint = self.environment.get("ANTHROPIC_BASE_URL") or DEFAULT_ENDPOINT
        model = (
            self.environment.get("AUTO_CODING_CLAUDE_MODEL")
            or self.environment.get("ANTHROPIC_MODEL")
            or DEFAULT_MODEL
        )
        has_api_key = bool(
            self.environment.get("ANTHROPIC_AUTH_TOKEN")
            or self.environment.get("ANTHROPIC_API_KEY")
        )
        ready = bool(installation.found and endpoint.strip() and model.strip() and has_api_key)
        return ModelSetupState(
            installation=installation,
            endpoint=endpoint,
            model=model,
            has_api_key=has_api_key,
            ready=ready,
        )

    def detect(self, command: str | None = None) -> ClaudeInstallation:
        """Find a real executable and verify it with a hidden `--version` call."""

        candidates = [command] if command else list(self._candidate_commands())
        last_error: str | None = None
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if not self._is_executable_file(path):
                last_error = "所选文件不是可直接运行的 Claude Code 程序。"
                continue
            resolved = str(path.resolve())
            try:
                completed = self._runner(
                    [resolved, "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                    **hidden_window_options(),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = f"Claude Code 检测失败：{exc}"
                continue
            if completed.returncode == 0:
                version = (completed.stdout or completed.stderr).strip().splitlines()
                logger.info("claude_detection_succeeded command=%s", resolved)
                return ClaudeInstallation(
                    found=True,
                    command=resolved,
                    version=version[0] if version else "已安装",
                )
            last_error = "Claude Code 无法正常运行，请重新安装或选择正确的 claude.exe。"
        logger.warning("claude_detection_failed")
        return ClaudeInstallation(
            found=False,
            error=last_error or "未检测到 Claude Code，请先安装或手动选择 claude.exe。",
        )

    def save(
        self,
        *,
        command: str,
        endpoint: str,
        model: str,
        api_key: str,
    ) -> ModelSetupState:
        """Validate and persist values; a blank key preserves an existing secret."""

        installation = self.detect(command.strip())
        if not installation.found or not installation.command:
            raise ModelSetupError(
                installation.error or "请选择可正常运行的 Claude Code 程序。"
            )
        endpoint = endpoint.strip().rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ModelSetupError("API 地址必须是完整的 http:// 或 https:// 地址。")
        model = model.strip()
        if not model:
            raise ModelSetupError("模型名称不能为空。")
        existing_key = self.environment.get("ANTHROPIC_AUTH_TOKEN") or self.environment.get(
            "ANTHROPIC_API_KEY"
        )
        api_key = api_key.strip()
        if not api_key and not existing_key:
            raise ModelSetupError("请输入 API Key。")

        values = {
            "AUTO_CODING_CLAUDE_COMMAND": installation.command,
            "AUTO_CODING_CLAUDE_MODEL": model,
            "ANTHROPIC_BASE_URL": endpoint,
            "ANTHROPIC_MODEL": model,
        }
        if api_key:
            values["ANTHROPIC_AUTH_TOKEN"] = api_key
        self.environment.set_many(values)
        get_settings.cache_clear()
        logger.info(
            "model_configuration_saved command=%s model=%s api_key_updated=%s",
            installation.command,
            model,
            bool(api_key),
        )
        return self.inspect(installation.command)

    def _candidate_commands(self) -> tuple[str | None, ...]:
        appdata = os.environ.get("APPDATA")
        return (
            self.environment.get("AUTO_CODING_CLAUDE_COMMAND"),
            self.environment.get("AUTO_TASK_AGENT_CLAUDE_CODE_COMMAND"),
            shutil.which("claude.exe"),
            shutil.which("claude"),
            r"D:\claude\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
            str(Path(appdata) / "npm" / "claude.exe") if appdata else None,
        )

    @staticmethod
    def _is_executable_file(path: Path) -> bool:
        if not path.is_file():
            return False
        return os.name != "nt" or path.suffix.casefold() in {".exe", ".com"}
