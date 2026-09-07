"""Hard execution boundaries; semantic choices deliberately stay with the model."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from autocoding_agent.core.models import AgentMode


@dataclass(frozen=True)
class PolicyProfile:
    tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    permission_mode: str = "dontAsk"


class ExecutionPolicy:
    """Map an already-approved mode to the tools Claude Code may use."""

    _READ_ONLY = ("Read", "Glob", "Grep")
    _SAFE_VALIDATION = (
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(python -m pytest:*)",
        "Bash(pytest:*)",
        "Bash(python -m ruff:*)",
        "Bash(ruff:*)",
        "Bash(npm test:*)",
        "Bash(npm run test:*)",
        "Bash(npm run lint:*)",
        "Bash(npm run typecheck:*)",
        "Bash(dotnet build:*)",
        "Bash(dotnet test:*)",
        "Bash(go test:*)",
        "Bash(cargo test:*)",
    )

    @staticmethod
    def _installed_python_validation() -> tuple[str, ...]:
        """允许当前 ACE Python 执行器运行 pytest/ruff，但不放开任意 Bash。

        Windows 桌面端通常由 ``pythonw.exe`` 启动，而用户看到并批准的命令一般使用同目录
        的 ``python.exe``。Claude Code 的 Bash 权限规则按命令前缀精确匹配，因此这里同时
        注册已解析出的真实执行器、同目录 python.exe 和 PATH 中的 Python；不使用会扩大
        权限范围的通配符。
        """

        candidates: list[str | Path | None] = [
            sys.executable,
            Path(sys.executable).with_name("python.exe"),
            shutil.which("python.exe"),
            shutil.which("python"),
        ]
        executables: set[str] = set()
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if not path.is_file():
                continue
            resolved = path.resolve()
            executables.add(str(resolved))
            executables.add(resolved.as_posix())

        rules: list[str] = []
        for executable in sorted(executables, key=str.casefold):
            forms = [executable]
            if " " in executable:
                forms.extend([f'"{executable}"', f"'{executable}'"])
            for form in forms:
                rules.extend(
                    (
                        f"Bash({form} -m pytest:*)",
                        f"Bash({form} -m ruff:*)",
                    )
                )
        return tuple(dict.fromkeys(rules))

    def profile(self, mode: AgentMode) -> PolicyProfile:
        if mode == AgentMode.INSPECT:
            return PolicyProfile(self._READ_ONLY, self._READ_ONLY)
        if mode == AgentMode.IMPLEMENT:
            tools = (*self._READ_ONLY, "Edit", "Write")
            return PolicyProfile(tools, tools)
        if mode == AgentMode.VERIFY:
            tools = (*self._READ_ONLY, "Bash")
            allowed = (
                *self._READ_ONLY,
                *self._SAFE_VALIDATION,
                *self._installed_python_validation(),
            )
            return PolicyProfile(tools, allowed)
        raise ValueError(f"Unsupported agent mode: {mode}")
