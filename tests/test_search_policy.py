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


def test_extension_glob_inside_explicit_candidate_subtree_is_allowed(tmp_path: Path) -> None:
    guard = BoundedSearchGuard(str(tmp_path))

    assert guard.inspect("Glob", {"pattern": "**/CKClient/**/*.cs"}) is None


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
            "path": "CKClient",
            "glob": "*.cs",
            "head_limit": 50,
        },
    )

    assert no_limit is not None and "head_limit" in no_limit.reason
    assert no_filter is not None and "glob 或 type" in no_filter.reason
    assert no_limit.retryable is True
    assert no_filter.retryable is True
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
    assert violation.retryable is False


def test_combined_search_call_budget_is_enforced(tmp_path: Path) -> None:
    guard = BoundedSearchGuard(str(tmp_path))

    for index in range(MAX_SEARCH_CALLS_PER_TURN):
        assert guard.inspect("Glob", {"pattern": f"**/Page{index}.cs"}) is None

    violation = guard.inspect("Glob", {"pattern": "**/OneMorePage.cs"})

    assert violation is not None
    assert "调用超过" in violation.reason
    assert violation.budget_exhausted
    assert not violation.retryable


def test_directories_share_total_budget_and_exact_file_reads_are_free(tmp_path):
    guard = BoundedSearchGuard(str(tmp_path))
    for directory in ("Business", "DAO"):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "Binding.cs").touch()
    for i in range(30):
        directory = "Business" if i % 2 else "DAO"
        assert guard.inspect("Read", {"file_path": f"{directory}/Binding.cs"}) is None
        assert guard.inspect("Grep", {
            "path": f"{directory}/Binding.cs", "pattern": "Save", "head_limit": 20,
        }, tool_use_id=f"search-{i}") is None
    assert guard.directory_calls == {"dao": 15, "business": 15}
    assert guard.progress_data()["repeat_count"] == 15
    assert guard.progress_data()["review_due"]
    assert guard.inspect("Glob", {"pattern": "**/Other.cs"}).budget_exhausted


def test_same_tool_event_is_not_double_counted_and_path_aliases_share_directory(tmp_path):
    (tmp_path / "Business").mkdir()
    guard = BoundedSearchGuard(str(tmp_path))
    for path in ("Business", "./Business", str(tmp_path / "Business")):
        assert guard.inspect("Glob", {"path": path, "pattern": "Binding.cs"},
                             tool_use_id=path) is None
    assert guard.inspect("Glob", {"path": "Business", "pattern": "Binding.cs"},
                         tool_use_id="Business") is None
    assert guard.calls == 3
    assert guard.directory_calls == {"business": 3}
    assert guard.last_repeat_count == 3
    assert not guard.progress_data()["review_due"]
    violation = guard.inspect("Glob", {"pattern": "**/*"}, tool_use_id="Business")
    assert violation is not None
    assert "禁止通配" in violation.reason


def test_root_overview_is_not_permission_to_scan_all_business_code(tmp_path):
    guard = BoundedSearchGuard(str(tmp_path))
    assert guard.inspect("Glob", {"pattern": "*.sln"}) is None
    assert guard.inspect("Grep", {
        "pattern": "Order", "path": ".", "glob": "*.cs", "head_limit": 20,
    }) is not None
    assert guard.inspect("Grep", {
        "pattern": "Order", "path": ".", "glob": "**/OrderController.cs", "head_limit": 20,
    }) is None
