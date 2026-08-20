# 版本发布与回退

本项目采用“一个经过验证的版本，只上传一次”的发布方式。Git 提交保存开发历史，带版本号的
Git tag 表示可以随时回退的稳定发布点。

## 发布规则

1. 完成当前版本功能并通过测试。
2. 更新 `pyproject.toml` 中的版本号，使用语义化版本 `主版本.次版本.修订号`。
3. 检查本次将要提交的文件，不提交 `.env`、缓存、运行日志或能力记忆。
4. 创建一个发布提交，提交信息使用 `release: v<版本号>`。
5. 创建同名 annotated tag：`v<版本号>`。
6. 每个版本只推送一次对应提交和 tag；已经发布的 tag 不修改、不覆盖、不强制推送。

发布前至少运行：

```powershell
D:\python\python.exe -m pytest -m "not live" -q
D:\python\python.exe -m ruff check src tests
```

## 安全回退

查看可回退版本：

```powershell
git tag --list --sort=-version:refname
```

只查看旧版本，不改变分支历史：

```powershell
git switch --detach v0.2.0
```

从旧版本建立一个可继续修复的新分支：

```powershell
git switch -c rollback/v0.2.0 v0.2.0
```

完成检查后回到主分支：

```powershell
git switch main
```

默认不使用 `git reset --hard`、强制推送或删除远端 tag，以确保任何已发布版本都可恢复。
