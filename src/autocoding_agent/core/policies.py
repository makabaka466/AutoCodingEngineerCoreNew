"""Hard execution boundaries; semantic choices deliberately stay with the model."""

from __future__ import annotations

from dataclasses import dataclass

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

    def profile(self, mode: AgentMode) -> PolicyProfile:
        if mode == AgentMode.INSPECT:
            return PolicyProfile(self._READ_ONLY, self._READ_ONLY)
        if mode == AgentMode.IMPLEMENT:
            tools = (*self._READ_ONLY, "Edit", "Write")
            return PolicyProfile(tools, tools)
        if mode == AgentMode.VERIFY:
            tools = (*self._READ_ONLY, "Bash")
            allowed = (*self._READ_ONLY, *self._SAFE_VALIDATION)
            return PolicyProfile(tools, allowed)
        raise ValueError(f"Unsupported agent mode: {mode}")
