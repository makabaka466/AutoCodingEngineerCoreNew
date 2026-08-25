# 版本发布与回退

从 `0.4.0` 开始，本项目每完成一次迭代，就向 GitHub 推送一次经过验证的提交。一次迭代应有
清晰目标、完整验证、同步文档和单一回退点，不把半完成状态上传到远端。

例如：

- 兼容修复：`v0.4.0 -> v0.4.1`，完成当前修复迭代后推送一次。
- 新能力或架构升级：`v0.4.x -> v0.5.0`，完成当前功能迭代后推送一次。
- 未完成的中间尝试可以本地提交，但不能作为一次完成迭代推送。

已经发布的 `v0.2.1` 作为规则确定前的基线保留，不删除、不改写。

## 迭代开发

1. 开始时明确本次迭代目标和范围；跨会话任务同步 `docs/tasks/` 任务卡。
2. 实现、测试并同步 README、架构、接口及 `docs/PROJECT_EXPERIENCE.md`。
3. 按语义化版本更新 `pyproject.toml`：兼容修复递增 patch，新能力递增 minor。
4. `.env`、缓存、运行日志、凭据、本地数据库和能力记忆始终不得提交。
5. 当前迭代收尾只形成一个边界清晰的远端提交；需要时可先在本地整理提交。

## 每次迭代上传

1. 检查当前工作区、diff、版本号和敏感信息。
2. 运行完整测试、静态检查与 `git diff --check`。
3. 创建一个描述当前迭代的提交，例如 `feat: add recoverable agent runtime`。
4. 向当前 GitHub 主分支推送一次，并确认远端提交与本地一致。
5. 只有明确的公开里程碑才创建 annotated tag；tag 一旦推送，不修改、不覆盖、不强制推送。

发布前至少运行：

```powershell
D:\python\python.exe -m pytest -m "not live" -q
D:\python\python.exe -m ruff check src tests
```

## 安全回退

每次迭代都可以通过远端提交回退。先查看提交：

```powershell
git log --oneline --decorate
```

从指定提交建立一个安全修复分支：

```powershell
git switch -c rollback/local-fix <commit-id>
```

如果该迭代同时创建了里程碑 tag，可查看：

```powershell
git tag --list --sort=-version:refname
```

从已发布批次建立回退分支：

```powershell
git switch -c rollback/v0.3.0 v0.3.0
```

完成检查后回到主分支：

```powershell
git switch main
```

默认不使用 `git reset --hard`、强制推送或删除远端 tag，以确保任何历史都可恢复。
