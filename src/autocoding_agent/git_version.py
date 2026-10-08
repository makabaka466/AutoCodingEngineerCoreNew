"""Bounded Git synchronization and explicitly reviewed publishing for a workspace."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from autocoding_agent.adapters.process_options import hidden_window_options


class GitVersionError(ValueError):
    """A Git state that requires a human decision before continuing."""


@dataclass(frozen=True)
class GitTarget:
    workspace: Path
    remote: str
    branch: str


class GitVersionService:
    """Only fast-forward a clean checkout; publish one reviewed working-tree snapshot."""

    def __init__(self, target: GitTarget) -> None:
        self.target = target
        root = target.workspace.resolve()
        if not root.is_dir() or not target.remote.strip() or not target.branch.strip():
            raise GitVersionError("请配置有效的本地项目、Git 远端和目标分支。")
        if target.remote.startswith("-") or any(c in target.remote for c in "\r\n\0"):
            raise GitVersionError("Git 远端地址格式无效。")
        self.root = root
        self._git("check-ref-format", "--branch", target.branch)
        top = Path(self._git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top != root:
            raise GitVersionError("配置的项目目录必须是 Git 仓库根目录。")
        self.remote_url = target.remote
        if not any(mark in target.remote for mark in (":", "/", "\\", "@")):
            self.remote_url = self._git("remote", "get-url", target.remote).stdout.strip()
        if urlsplit(self.remote_url).password:
            raise GitVersionError("远端地址不可包含密码；请使用 Git 凭据管理器。")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            environment = os.environ.copy()
            environment["GIT_TERMINAL_PROMPT"] = "0"
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=300 if args[0] in {"fetch", "push"} else 90,
                check=False, env=environment, **hidden_window_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitVersionError(f"Git 命令无法完成：{type(exc).__name__}") from exc
        if result.returncode:
            # Git stderr may contain authenticated remote URLs; never surface it.
            raise GitVersionError(f"Git {args[0]} 失败；请检查远端、凭据和仓库状态。")
        return result

    def _branch(self) -> None:
        if self._git("symbolic-ref", "--short", "HEAD").stdout.strip() != self.target.branch:
            raise GitVersionError(f"当前分支必须是 {self.target.branch}。")

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _remote_head(self) -> str:
        self._git("fetch", "--no-tags", self.target.remote, self.target.branch)
        return self._git("rev-parse", "FETCH_HEAD").stdout.strip()

    def _status(self) -> bytes:
        # NUL output is decoded without normalization; the digest binds exact paths and status.
        return self._git(
            "status", "--porcelain=v1", "--no-renames", "-z", "--untracked-files=all"
        ).stdout.encode()

    def _fingerprint(self, head: str, status: bytes) -> str:
        target = (self.remote_url + "\0" + self.target.branch).encode()
        digest = hashlib.sha256(target + b"\0" + head.encode() + b"\0" + status)
        digest.update(self._git("diff", "--binary", "HEAD", "--").stdout.encode())
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
        for name in (part for part in untracked.split("\0") if part):
            path = self.root / name
            digest.update(name.encode())
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                with path.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
        return digest.hexdigest()

    def sync(self) -> str:
        """Fetch and fast-forward only if the current branch is clean."""
        self._branch()
        if self._status():
            raise GitVersionError("本地存在未提交修改，请先处理后再拉取最新代码。")
        before = self._head()
        remote = self._remote_head()
        if before == remote:
            return before
        local_ahead = True
        try:
            self._git("merge-base", "--is-ancestor", remote, before)
        except GitVersionError:
            local_ahead = False
        if local_ahead:
            raise GitVersionError("本地有待推送提交，请先审阅并推送，再更新代码。")
        try:
            self._git("merge-base", "--is-ancestor", before, remote)
        except GitVersionError as exc:
            raise GitVersionError("本地与远端分支已分叉，请人工处理后再更新。") from exc
        self._git("merge", "--ff-only", remote)
        return self._head()

    def preview(self) -> dict:
        """Return reviewable files and a short-lived fingerprint for explicit publish."""
        self._branch()
        status = self._status()
        if not status:
            return self._pending_push()
        if self._git("ls-files", "-u").stdout:
            raise GitVersionError("存在未解决的合并冲突。")
        entries = [part for part in status.decode().split("\0") if part]
        paths = [entry[3:] for entry in entries]
        head = self._head()
        fingerprint = self._fingerprint(head, status)
        return {"branch": self.target.branch, "remote": self.target.remote,
                "head": head, "files": paths, "fingerprint": fingerprint,
                "pending_push": False}

    def _pending_push(self) -> dict:
        head = self._head()
        remote = self._remote_head()
        if head == remote:
            raise GitVersionError("没有需要提交或推送的修改。")
        try:
            self._git("merge-base", "--is-ancestor", remote, head)
        except GitVersionError as exc:
            raise GitVersionError("本地提交与远端已分叉，请人工处理。") from exc
        ahead = self._git("rev-list", "--count", f"{remote}..{head}").stdout.strip()
        if ahead != "1":
            raise GitVersionError("本地存在多个待推送提交，请人工审阅后处理。")
        summary = self._git("show", "-s", "--format=%s", "HEAD").stdout.strip()
        files = self._git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        fingerprint = hashlib.sha256(
            (self.remote_url + "\0" + self.target.branch + "\0" + head + "\0" + remote).encode()
        ).hexdigest()
        return {"branch": self.target.branch, "remote": self.target.remote,
                "head": head, "files": files.stdout.splitlines(),
                "fingerprint": fingerprint, "pending_push": True, "summary": summary}

    def publish(self, summary: str, fingerprint: str) -> dict:
        """Commit exactly the reviewed snapshot, then push without force."""
        summary = summary.strip()
        if not summary or "\n" in summary or len(summary) > 120:
            raise GitVersionError("请填写 1 至 120 字的单行修改摘要。")
        reviewed = self.preview()
        if fingerprint != reviewed["fingerprint"]:
            raise GitVersionError("待提交文件已变化，请重新查看修改清单。")
        if reviewed["pending_push"]:
            if summary != reviewed["summary"] or self._pending_push()["fingerprint"] != fingerprint:
                raise GitVersionError("待推送提交已变化，请重新审阅。")
            self._git("push", self.target.remote, f"HEAD:refs/heads/{self.target.branch}")
            return {"commit": reviewed["head"], "branch": self.target.branch,
                    "remote": self.target.remote, "summary": summary}
        remote = self._remote_head()
        if remote != reviewed["head"]:
            raise GitVersionError("远端代码已变化，请先处理差异并重新审阅。")
        if self.preview()["fingerprint"] != fingerprint:
            raise GitVersionError("拉取远端期间文件已变化，请重新查看修改清单。")
        self._git("add", "--all")
        if not self._git("diff", "--cached", "--name-only").stdout:
            raise GitVersionError("没有可提交的文件。")
        staged_tree = self._git("write-tree").stdout.strip()
        self._git("commit", "-m", summary)
        commit = self._head()
        if self._git("rev-parse", "HEAD^{tree}").stdout.strip() != staged_tree:
            raise GitVersionError("提交钩子更改了已审阅内容，已停止推送；请检查本地提交。")
        try:
            self._git("push", self.target.remote, f"HEAD:refs/heads/{self.target.branch}")
        except GitVersionError as exc:
            raise GitVersionError(
                f"本地提交 {commit[:12]} 已创建但推送失败；请审阅待推送提交后重试。"
            ) from exc
        return {"commit": commit, "branch": self.target.branch,
                "remote": self.target.remote, "summary": summary}
