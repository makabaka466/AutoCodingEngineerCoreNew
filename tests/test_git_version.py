"""Real local Git repositories exercise the update and publish boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autocoding_agent.git_version import GitTarget, GitVersionError, GitVersionService


def git(where: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=where, capture_output=True, text=True,
        encoding="utf-8", check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    first = tmp_path / "first"
    first.mkdir()
    git(first, "init", "-b", "main")
    git(first, "config", "user.name", "Test User")
    git(first, "config", "user.email", "test@example.invalid")
    (first / "file.txt").write_text("one", encoding="utf-8")
    git(first, "add", "--all")
    git(first, "commit", "-m", "initial")
    git(first, "remote", "add", "origin", str(remote))
    git(first, "push", "origin", "HEAD:refs/heads/main")
    second = tmp_path / "second"
    git(tmp_path, "clone", "-b", "main", str(remote), str(second))
    git(second, "config", "user.name", "Test User")
    git(second, "config", "user.email", "test@example.invalid")
    return first, second, remote


def test_sync_fast_forwards_and_refuses_dirty_checkout(repositories):
    first, second, remote = repositories
    service = GitVersionService(GitTarget(second, str(remote), "main"))
    (first / "file.txt").write_text("two", encoding="utf-8")
    git(first, "add", "--all")
    git(first, "commit", "-m", "remote change")
    git(first, "push", "origin", "HEAD:refs/heads/main")
    assert service.sync() == git(first, "rev-parse", "HEAD")
    assert (second / "file.txt").read_text(encoding="utf-8") == "two"
    (second / "local.txt").write_text("untracked", encoding="utf-8")
    with pytest.raises(GitVersionError, match="未提交"):
        service.sync()


def test_publish_requires_reviewed_snapshot_and_current_remote(repositories):
    first, second, remote = repositories
    service = GitVersionService(GitTarget(second, str(remote), "main"))
    (second / "new.txt").write_text("new", encoding="utf-8")
    preview = service.preview()
    assert preview["files"] == ["new.txt"]
    (second / "new.txt").write_text("newer", encoding="utf-8")
    with pytest.raises(GitVersionError, match="文件已变化"):
        service.publish("add file", preview["fingerprint"])
    preview = service.preview()
    with pytest.raises(GitVersionError, match="已变化"):
        service.publish("add file", "0" * 64)
    (first / "remote.txt").write_text("remote", encoding="utf-8")
    git(first, "add", "--all")
    git(first, "commit", "-m", "other work")
    git(first, "push", "origin", "HEAD:refs/heads/main")
    with pytest.raises(GitVersionError, match="远端代码已变化"):
        service.publish("add file", preview["fingerprint"])
    assert git(second, "status", "--porcelain").startswith("?? new.txt")


def test_publish_pushes_one_reviewed_commit(repositories):
    _, second, remote = repositories
    service = GitVersionService(GitTarget(second, str(remote), "main"))
    (second / "new.txt").write_text("new", encoding="utf-8")
    preview = service.preview()
    result = service.publish("add new file", preview["fingerprint"])
    assert result["commit"] == git(second, "rev-parse", "HEAD")
    assert result["commit"] == git(remote, "rev-parse", "refs/heads/main")
    assert git(second, "status", "--porcelain") == ""


def test_reviewed_local_commit_can_retry_push(repositories):
    _, second, remote = repositories
    service = GitVersionService(GitTarget(second, str(remote), "main"))
    (second / "new.txt").write_text("new", encoding="utf-8")
    git(second, "add", "--all")
    git(second, "commit", "-m", "add new file")
    pending = service.preview()
    assert pending["pending_push"] is True
    assert pending["files"] == ["new.txt"]
    with pytest.raises(GitVersionError, match="待推送提交已变化"):
        service.publish("different summary", pending["fingerprint"])
    result = service.publish("add new file", pending["fingerprint"])
    assert result["commit"] == git(remote, "rev-parse", "refs/heads/main")
