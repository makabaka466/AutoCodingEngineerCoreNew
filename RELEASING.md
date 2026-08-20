# 版本发布与回退

本项目以版本号的中间位作为公开发布批次。补丁位用于本地迭代；Git 本地提交保存每次小改动，
远端 tag 只表示经过完整验证的批次版本。

例如：

- `v0.2.1 -> v0.2.2 -> v0.2.x`：只在本地开发和提交，不推送、不创建远端 tag。
- `v0.2.x -> v0.3.0`：中间位从 `2` 升到 `3`，完整验证后只发布一次。
- 后续同理：`v0.3.x` 本地迭代，直到发布 `v0.4.0`。

已经发布的 `v0.2.1` 作为规则确定前的基线保留，不删除、不改写。

## 本地迭代

1. 每个完整的小改动可以创建本地 Git 提交，便于定位和回退。
2. 补丁位可以按需要递增，但 `v0.2.x` 期间不向 GitHub push，也不创建发布 tag。
3. `.env`、缓存、运行日志和能力记忆始终不得提交。
4. 本地提交不使用 `release:` 前缀；该前缀只用于真正的批次发布。

## 批次发布

只有中间版本位递增时执行发布：

1. 将 `pyproject.toml` 版本设为新的批次起点，例如 `0.3.0`。
2. 检查自上次远端版本以来的全部本地提交和文件。
3. 运行完整测试与静态检查。
4. 创建一个发布提交：`release: v0.3.0`。
5. 创建同名 annotated tag：`v0.3.0`。
6. 用一次 atomic push 同时上传 `main` 和 tag。
7. 已发布的 tag 不修改、不覆盖、不强制推送。

发布前至少运行：

```powershell
D:\python\python.exe -m pytest -m "not live" -q
D:\python\python.exe -m ruff check src tests
```

## 安全回退

本地小版本通过提交回退。先查看提交：

```powershell
git log --oneline --decorate
```

从指定提交建立一个安全修复分支：

```powershell
git switch -c rollback/local-fix <commit-id>
```

查看远端可回退批次：

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
