"""Read-only host observation of a Git workspace before and after execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from autocoding_agent.adapters.process_options import hidden_window_options
from autocoding_agent.core.artifacts.models import WorkspaceSnapshot

_MAX_CAPTURE_CHARS = 1_500_000


class GitWorkspaceObserver:
    """Capture status plus staged/unstaged diff without reading untracked contents."""

    def capture(self, workspace: str | Path) -> WorkspaceSnapshot:
        root = Path(workspace).resolve()
        probe = self._run(root, "rev-parse", "--is-inside-work-tree")
        if probe.returncode != 0 or probe.stdout.strip().casefold() != "true":
            return WorkspaceSnapshot(
                is_git=False,
                dirty=False,
                status_entries=(),
                related_paths=(),
                patch="",
                git_commit=None,
                error="Workspace is not inside a Git worktree.",
            )

        status_result = self._run(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
        )
        if status_result.returncode != 0:
            return WorkspaceSnapshot(
                is_git=True,
                dirty=False,
                status_entries=(),
                related_paths=(),
                patch="",
                git_commit=self._commit(root),
                error=self._compact_error(status_result),
            )

        status_entries, paths = self._parse_status(status_result.stdout)
        unstaged = self._run(root, "diff", "--binary", "--no-ext-diff", "--no-color", "--", ".")
        staged = self._run(
            root,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-color",
            "--cached",
            "--",
            ".",
        )
        patch_parts: list[str] = []
        errors: list[str] = []
        if staged.returncode == 0 and staged.stdout:
            patch_parts.extend(["# Staged changes", staged.stdout])
        elif staged.returncode != 0:
            errors.append(self._compact_error(staged))
        if unstaged.returncode == 0 and unstaged.stdout:
            patch_parts.extend(["# Unstaged changes", unstaged.stdout])
        elif unstaged.returncode != 0:
            errors.append(self._compact_error(unstaged))
        patch = "\n".join(patch_parts)
        truncated = len(patch) > _MAX_CAPTURE_CHARS
        if truncated:
            patch = patch[:_MAX_CAPTURE_CHARS] + "\n# [TRUNCATED BY HOST]\n"
        return WorkspaceSnapshot(
            is_git=True,
            dirty=bool(status_entries),
            status_entries=tuple(status_entries),
            related_paths=tuple(dict.fromkeys(paths)),
            patch=patch,
            git_commit=self._commit(root),
            truncated=truncated,
            error="; ".join(filter(None, errors)) or None,
        )

    @staticmethod
    def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *arguments],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                **hidden_window_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(
                ["git", *arguments],
                returncode=1,
                stdout="",
                stderr=str(exc),
            )

    def _commit(self, root: Path) -> str | None:
        result = self._run(root, "rev-parse", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _parse_status(output: str) -> tuple[list[str], list[str]]:
        chunks = [chunk for chunk in output.split("\0") if chunk]
        entries: list[str] = []
        paths: list[str] = []
        index = 0
        while index < len(chunks):
            entry = chunks[index]
            entries.append(entry)
            if len(entry) >= 4:
                paths.append(entry[3:].replace("\\", "/"))
            status = entry[:2]
            if ("R" in status or "C" in status) and index + 1 < len(chunks):
                index += 1
                entries.append(chunks[index])
                paths.append(chunks[index].replace("\\", "/"))
            index += 1
        return entries, paths

    @staticmethod
    def _compact_error(result: subprocess.CompletedProcess[str]) -> str:
        return " ".join((result.stderr or result.stdout or "Git command failed.").split())[:500]
