"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_claude_command() -> str:
    """Prefer a directly executable Claude binary, especially on Windows."""

    candidates = [
        os.getenv("AUTO_TASK_AGENT_CLAUDE_CODE_COMMAND"),
        shutil.which("claude.exe"),
        shutil.which("claude"),
        r"D:\claude\node_modules\@anthropic-ai\claude-code\bin\claude.exe",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        if os.name == "nt" and path.suffix.casefold() not in {".exe", ".com"}:
            continue
        return str(path.resolve())
    return "claude"


class Settings(BaseSettings):
    """Configuration that may change between machines or model providers."""

    claude_command: str = Field(default_factory=_default_claude_command)
    claude_model: str = "deepseek-v4-pro"
    claude_timeout_seconds: int = Field(default=600, ge=10)
    max_budget_usd: float | None = Field(default=None, gt=0)
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".autocoding-agent")

    model_config = SettingsConfigDict(
        env_prefix="AUTO_CODING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-in-practice settings object per process."""

    return Settings()
