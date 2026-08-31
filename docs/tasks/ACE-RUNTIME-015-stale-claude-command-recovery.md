# 任务记录：ACE-RUNTIME-015 Claude启动路径恢复与错误分类

- 任务编号：`ACE-RUNTIME-015`
- 当前状态：`done`
- 下一步责任方：用户重启桌面客户端并新建异常任务进行真实模型复测
- 创建日期：2026-08-31
- 用户授权：修复异常处理页面错误地报告找不到Claude Code；完成后按既有规则同步工程经验并推送一次。
- 修改前回退点：`v0.7.1` / `47f56c8`

## 1. 目标

1. 双击启动时始终加载配置页保存在Windows用户环境中的最新Claude路径和模型配置；
2. Runtime遇到父进程遗留的失效路径时，重新解析已安装的真实`claude.exe`；
3. 分别报告Claude程序缺失、项目目录缺失和其他系统启动错误；
4. 日志记录最终采用的Claude路径与工作区，但不记录Prompt或密钥；
5. 开发与异常流程共用修复，不改变现有工具权限和审批边界。

## 2. 已确认根因与证据

- 2026-08-31 09:57，配置检测成功运行真实`claude.exe --version`；
- 10:01，异常Runtime在进程创建阶段得到`FileNotFoundError`，旧实现统一翻译为“executable was not found”；
- 真实exe、异常Session工作区均存在，同Python、同cwd、隐藏窗口参数的最小进程复现通过；
- 已复现配置分叉：失效的进程级`AUTO_CODING_CLAUDE_COMMAND`会被Pydantic Settings直接接受，
  配置检测则跳过失效值并找到用户配置/默认真实exe；
- `start.ps1`只在进程值为空时导入用户值，使长时间运行的Explorer或终端可以把旧的非空值传给客户端。

## 3. 验收标准

- 失效进程路径存在时，Runtime恢复到已验证的用户级或本机真实exe；
- 启动脚本用非空用户配置覆盖继承的旧进程配置；
- exe缺失与workspace缺失返回不同错误；
- Runtime开始日志包含脱敏安全的最终command和workspace；
- 聚焦测试、完整非live测试、Ruff、compileall和`git diff --check`通过；
- 版本更新为`0.7.2`，同步README、架构、接口和工程经验，提交、tag并推送GitHub一次。

## 4. 修改与验证记录

- `start.ps1`改为使用非空Windows用户值刷新当前进程，不再保留继承的旧非空配置；
- Runtime只对真实process解析并恢复Claude路径，注入测试适配器保持原样；
- exe和workspace缺失具有独立中文错误，Runtime日志记录最终command/workspace；
- 失效路径对照复现：`C:\stale\claude.exe`成功恢复到本机真实exe，未调用模型；
- 最终聚焦回归：`28 passed`；完整非live回归：`186 passed in 27.42s`；
- Ruff、compileall和`git diff --check`通过；版本、README、架构、接口和工程经验已同步到`0.7.2`；
- 回退方式：切换到`v0.7.1`或提交`47f56c8`建立安全回退分支。
