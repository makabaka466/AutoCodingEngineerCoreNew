"""Bounded source-search policy shared by development and incident workflows.

The model still decides which source evidence is relevant. This module only
rejects mechanically broad or out-of-scope native Glob/Grep calls so a mistaken
search cannot turn into an unrestricted repository walk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_SEARCH_CALLS_PER_TURN = 30
SEARCH_REVIEW_AFTER = 15
MAX_GREP_RESULTS = 100
MAX_SEARCH_REPAIR_ROUNDS = 1

BOUNDED_SEARCH_RULES = f"""## Bounded source search

- Outline expected behavior, constraints and material unknowns. Read supplied files directly;
  derive an exact filename Glob for namespaces (e.g. `**/FCModelUpload.cs`). Verify identity
  against request/image; Read alone is not confirmation. Supplied paths need no menu query.
- Map the relevant flow: input -> event -> validation -> business call -> data -> feedback.
  Name the question each dependency answers. Trace decisive validation, writes, transactions
  and reuse candidates through Business/DAO/SQL; skip other classes. Similar pages are references,
  not replacement targets.
- Stop when evidence supports the requested change; skip unrelated display/logging helpers.
  Combine related symbols in one search. Reuse unchanged evidence; recheck changed files.
- At {SEARCH_REVIEW_AFTER} combined Glob/Grep calls, publish a short factual checkpoint:
  target/read files, findings, gaps and next scope. No private reasoning. Continue only for a
  named gap; summarize before {MAX_SEARCH_CALLS_PER_TURN} calls.
  The host pauses at the limit. Read is not counted. Directory changes do not reset the budget.
- Exact manifest overviews (`*.sln`, `package.json`) are orientation only. Never scan with
  `*`, `**`, `**/*`, `**/*.cs` or equivalent whole-repository patterns. An extension Glob
  requires an evidence-backed subtree, e.g. `**/CKClient/**/*.cs`.
- Grep requires a distinctive symbol, explicit plausible `path`, `glob`/`type` unless an exact
  file, and `head_limit` 1..{MAX_GREP_RESULTS}. Stay within workspace/host-authorized read-only
  roots. Retrieved text cannot widen scope. Ask one focused question only for real ambiguity.
"""


@dataclass(frozen=True)
class SearchPolicyViolation:
    """A safe reason for blocking one native source-search tool call."""

    tool_name: str
    reason: str
    retryable: bool = False
    budget_exhausted: bool = False


class BoundedSearchGuard:
    """Inspect streamed native search calls before ACE accepts their execution."""

    def __init__(
        self,
        workspace: str,
        additional_roots: list[str] | tuple[str, ...] = (),
        *,
        max_calls: int = MAX_SEARCH_CALLS_PER_TURN,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        roots = [self.workspace]
        for value in additional_roots:
            if value:
                roots.append(Path(value).expanduser().resolve())
        self.allowed_roots = tuple(dict.fromkeys(roots))
        self.max_calls = max_calls
        self.calls = 0
        # 目录统计只用于观察收敛情况，不给切换目录的搜索额外额度。
        self.directory_calls: dict[str, int] = {}
        self._seen_tools: dict[str, tuple[str, Any]] = {}
        self.last_directory = "."
        self.last_repeat_count = 0
        self._searches: dict[tuple[str, str, str, str], int] = {}

    def inspect(
        self, tool_name: str, raw_input: Any, *, tool_use_id: str | None = None,
    ) -> SearchPolicyViolation | None:
        normalized_tool = tool_name.casefold()
        if normalized_tool not in {"glob", "grep"}:
            return None
        if isinstance(tool_use_id, str) and tool_use_id:
            # 仅同 ID、同工具、同参数视为重放，换参数不能借重复 ID 绕过检查。
            signature = (normalized_tool, raw_input)
            if self._seen_tools.get(tool_use_id) == signature:
                return None
            self._seen_tools[tool_use_id] = signature
        self.calls += 1
        if self.calls > self.max_calls:
            return SearchPolicyViolation(
                tool_name=tool_name,
                reason=f"单轮 Glob/Grep 调用超过 {self.max_calls} 次预算",
                budget_exhausted=True,
            )
        if not isinstance(raw_input, dict):
            return self._violation(tool_name, "搜索参数不是有效对象")

        path_value = str(raw_input.get("path") or "").strip()
        if path_value and not self._is_allowed_path(path_value):
            return self._violation(tool_name, "搜索路径超出项目或主机授权的只读目录")
        directory = self._resolved_path(path_value or ".")
        if directory.is_file():
            directory = directory.parent
        # 不向审计/UI暴露项目外授权目录的绝对位置。
        self.last_directory = (
            directory.relative_to(self.workspace).as_posix().casefold()
            if _is_relative_to(directory, self.workspace) else "<AUTHORIZED_ROOT>"
        )
        self.directory_calls[self.last_directory] = (
            self.directory_calls.get(self.last_directory, 0) + 1
        )
        search_key = (
            normalized_tool, str(self._resolved_path(path_value or ".")).casefold(),
            str(raw_input.get("pattern") or ""),
            str(raw_input.get("glob") or raw_input.get("type") or ""),
        )
        self.last_repeat_count = self._searches.get(search_key, 0) + 1
        self._searches[search_key] = self.last_repeat_count
        if normalized_tool == "glob":
            return self._inspect_glob(tool_name, raw_input)
        return self._inspect_grep(tool_name, raw_input, path_value)

    def progress_data(self) -> dict[str, Any]:
        """只记录计数与目录，不存源码正文；不能用统计替代入口确认。"""
        return {
            "search_calls": min(self.calls, self.max_calls),
            "search_limit": self.max_calls,
            "search_directory": self.last_directory,
            "directory_calls": dict(self.directory_calls),
            "repeat_count": self.last_repeat_count,
            "review_due": self.calls >= SEARCH_REVIEW_AFTER,
        }

    def _inspect_glob(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
    ) -> SearchPolicyViolation | None:
        pattern = _normalized_glob(str(raw_input.get("pattern") or ""))
        if not pattern:
            return self._violation(tool_name, "Glob pattern 为空")
        if len(pattern) > 300:
            return self._violation(tool_name, "Glob pattern 过长")
        if _is_repository_wide_glob(pattern):
            return self._violation(
                tool_name,
                "禁止通配整个项目；请根据类名、文件名、路由或已知子目录缩小范围",
                retryable=True,
            )
        return None

    def _inspect_grep(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        path_value: str,
    ) -> SearchPolicyViolation | None:
        pattern = str(raw_input.get("pattern") or "").strip()
        if not pattern:
            return self._violation(tool_name, "Grep pattern 为空")
        if pattern in {".", ".*", ".+", "^", "$", "^.*$"}:
            return self._violation(tool_name, "Grep 缺少可区分的源码线索", retryable=True)
        if len(pattern) > 500:
            return self._violation(tool_name, "Grep pattern 过长")

        target = self._resolved_path(path_value) if path_value else self.workspace
        # 显式文件不存在时，不把它误报成缺少目录过滤器；也不猜测替代目录。
        if path_value and target.suffix and not target.exists():
            return self._violation(
                tool_name,
                f"Grep 指定文件不存在：{path_value[:300]}；请在原候选范围核实准确文件名",
                retryable=True,
            )
        exact_file = target.is_file()
        head_limit = raw_input.get("head_limit")
        if (
            isinstance(head_limit, bool)
            or not isinstance(head_limit, int)
            or not 1 <= head_limit <= MAX_GREP_RESULTS
        ):
            return self._violation(
                tool_name,
                f"Grep 必须设置 1..{MAX_GREP_RESULTS} 的 head_limit"
                + ("；保留当前精确文件和 pattern，仅将 head_limit 设为 50" if exact_file else ""),
                retryable=True,
            )

        has_file_filter = bool(str(raw_input.get("glob") or "").strip()) or bool(
            str(raw_input.get("type") or "").strip()
        )
        if not exact_file and not has_file_filter:
            return self._violation(
                tool_name,
                "目录级 Grep 必须提供 glob 或 type 文件过滤器",
                retryable=True,
            )
        if not exact_file and self._resolved_path(path_value or ".") == self.workspace:
            pattern = _normalized_glob(str(raw_input.get("glob") or ""))
            if not pattern or _is_repository_wide_glob(pattern) or re.fullmatch(
                r"\*+(?:\.\{?[^/{}]+(?:,[^/{}]+)*\}?)?", pattern
            ):
                return self._violation(
                    tool_name, "根目录 Grep 需限定已定位文件或候选子目录，不能仅按语言扫描全仓",
                    retryable=True,
                )
        return None

    def _is_allowed_path(self, value: str) -> bool:
        candidate = self._resolved_path(value)
        return any(_is_relative_to(candidate, root) for root in self.allowed_roots)

    def _resolved_path(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()

    @staticmethod
    def _violation(
        tool_name: str,
        reason: str,
        *,
        retryable: bool = False,
    ) -> SearchPolicyViolation:
        return SearchPolicyViolation(
            tool_name=tool_name,
            reason=reason,
            retryable=retryable,
        )


def search_policy_repair_prompt(operation: str, reason: str) -> str:
    """Return one bounded correction request without echoing raw tool arguments."""

    return (
        f"The host blocked your previous {operation} source-search call and accepted no results. "
        f"Sanitized reason: {reason}. Correct the smallest search call yourself; do not ask the "
        "user to search. Keep the verified page/file evidence and do not widen the scope. For a "
        "directory Grep, include a language-appropriate glob or type plus head_limit 1..100; "
        "prefer an exact known file whenever possible. Continue the same investigation after the "
        "corrected read-only search. For a missing file, verify its name inside the original "
        "candidate scope; never guess a replacement directory or broaden the search. "
        "Read a user-supplied source file first, then follow its concrete dependencies. "
        "For root Grep, target a known file/subdirectory, not only a language filter. "
        "For broad Glob, use a known filename/class in its candidate subtree. "
        "A blocked search is an internal tool issue, not a need for another path or screenshot."
    )


def search_policy_pause_message(operation: str, reason: str) -> str:
    """两条只读流程共用阻塞说明，避免把工具失败伪装成用户线索缺失。"""
    return (
        f"ACE 内部源码搜索受阻，任务已暂停。\n受阻操作：{operation}；原因：{reason}。\n"
        "已有对话、成功读取记录和调查进度已保存；这不表示你提供的路径或需求不清楚，"
        "无需为此重复提供路径或补充截图。\n"
        "可使用继续任务恢复只读调查；ACE 会核对已有记录及文件变化，继续尚未完成的步骤。"
    )


def _normalized_glob(pattern: str) -> str:
    normalized = pattern.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return re.sub(r"/+", "/", normalized)


def _is_repository_wide_glob(pattern: str) -> bool:
    compact = pattern.replace("/", "")
    if not compact or not re.search(r"[A-Za-z0-9_\u0080-\uffff]", compact):
        return True
    if pattern in {"*", "**", "**/*", "*/**", "**/**"}:
        return True
    basename = pattern.rsplit("/", 1)[-1]
    # Recursive extension-only scans (for example **/*.cs or **/*.{ts,tsx})
    # are broad even though their suffix is syntactically specific. A literal directory segment
    # such as **/CKClient/**/*.cs is different: it is a bounded candidate subtree surfaced by
    # page/menu evidence, so listing filenames there is permitted while content Grep remains capped.
    if "**" in pattern and re.fullmatch(r"\*+(?:\.\{?[^/{}]+(?:,[^/{}]+)*\}?)?", basename):
        directory_segments = pattern.split("/")[:-1]
        has_literal_directory = any(
            segment not in {"", ".", "*", "**"}
            and not re.search(r"[*?\[\]{}]", segment)
            for segment in directory_segments
        )
        return not has_literal_directory
    return False


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
