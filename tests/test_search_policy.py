"""Host boundary tests for native Glob/Grep source discovery."""

from __future__ import annotations

from pathlib import Path

from autocoding_agent.core.search_policy import (
    MAX_SEARCH_CALLS_PER_TURN,
    BoundedSearchGuard,
)


def test_exact_filename_glob_is_allowed_while_repository_walk_is_blocked(
    tmp_path: Path,
) -> None:
    guard = BoundedSearchGuard(str(tmp_path))

    assert guard.inspect("Glob", {"pattern": "**/FCModelUpload.cs"}) is None
    violation = guard.inspect("Glob", {"pattern": "**/*"})

    assert violation is not None
    assert "禁止通配整个项目" in violation.reason


def test_recursive_extension_only_glob_is_blocked(tmp_path: Path) -> None:
    guard = BoundedSearchGuard(str(tmp_path))

    violation = guard.inspect("Glob", {"pattern": "**/*.cs"})

    assert violation is not None
    assert "禁止通配整个项目" in violation.reason


def test_grep_requires_bounded_output_and_file_scope(tmp_path: Path) -> None:
    guard = BoundedSearchGuard(str(tmp_path))

    no_limit = guard.inspect(
        "Grep",
        {"pattern": "FCModelUpload", "path": ".", "glob": "*.cs"},
    )
    no_filter = guard.inspect(
        "Grep",
        {"pattern": "FCModelUpload", "path": ".", "head_limit": 50},
    )
    allowed = guard.inspect(
        "Grep",
        {
            "pattern": "FCModelUpload",
            "path": ".",
            "glob": "*.cs",
            "head_limit": 50,
        },
    )

    assert no_limit is not None and "head_limit" in no_limit.reason
    assert no_filter is not None and "glob 或 type" in no_filter.reason
    assert allowed is None


def test_exact_file_grep_does_not_require_extension_filter(tmp_path: Path) -> None:
    source = tmp_path / "FCModelUpload.cs"
    source.touch()
    guard = BoundedSearchGuard(str(tmp_path))

    assert (
        guard.inspect(
            "Grep",
            {
                "pattern": "Load",
                "path": str(source),
                "head_limit": 20,
            },
        )
        is None
    )


def test_search_path_cannot_escape_authorized_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guard = BoundedSearchGuard(str(workspace))

    violation = guard.inspect(
        "Grep",
        {
            "pattern": "secret",
            "path": str(tmp_path),
            "glob": "*.txt",
            "head_limit": 10,
        },
    )

    assert violation is not None
    assert "超出项目" in violation.reason


def test_combined_search_call_budget_is_enforced(tmp_path: Path) -> None:
    guard = BoundedSearchGuard(str(tmp_path))

    for index in range(MAX_SEARCH_CALLS_PER_TURN):
        assert guard.inspect("Glob", {"pattern": f"**/Page{index}.cs"}) is None

    violation = guard.inspect("Glob", {"pattern": "**/OneMorePage.cs"})

    assert violation is not None
    assert "调用超过" in violation.reason
