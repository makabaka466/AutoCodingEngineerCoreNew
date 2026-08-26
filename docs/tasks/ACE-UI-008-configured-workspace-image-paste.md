# 任务记录：ACE-UI-008 配置化项目路径与异常截图粘贴

- 任务编号：`ACE-UI-008`
- 当前状态：`completed`
- 下一步责任方：用户验收桌面端真实剪贴板图片与目标模型识图效果
- 创建日期：2026-08-26
- 用户授权：项目路径应保存在系统配置中；异常处理对话删除独立“异常页面”栏，允许用户把异常界面截图直接粘贴进对话框。
- 目标：减少任务输入区重复配置，让异常截图成为可持久、可审计、模型真实可读的只读附件。

## 验收标准

- 系统配置增加项目路径，支持浏览、校验、保存和随时更换；
- 新开发/异常任务从配置读取项目路径，主对话区不再显示路径输入与浏览按钮；
- 已有 Session 继续使用自己保存的 workspace，不受配置切换影响；
- 异常流程不再展示“异常页面”独立输入栏，页面名称、路由等文字直接写在对话中；
- 异常输入框支持粘贴剪贴板图片，并展示待发送附件，可移除；
- 粘贴图片保存到应用数据目录的隔离附件目录，不写目标代码仓库；
- 每次最多发送 5 张图片、单张不超过 10 MiB，只接受 PNG/JPEG/WebP/GIF；
- Runtime 只读挂载附件所在的隔离目录，消息中包含准确文件路径和“不可信证据”说明；
- 图片内容不得被当作系统指令，附件路径必须真实、规范化且为受支持图片；
- 发送成功后清空待发送附件；文本粘贴和开发流程原有粘贴行为不受影响；
- 自动化测试、完整非 live 回归、Ruff 和 diff 检查通过；
- 项目经验同步更新，形成 `v0.5.5` 提交/tag 并向 `origin` 推送一次。

## 当前状态与关键发现

- 当前项目路径是对话 composer 中的临时输入，默认值为进程 cwd，没有独立持久化配置；
- 当前异常流程使用 `page_hint_entry`，并把该值传给 `IncidentApplication.start()`；
- Runtime 只挂载 Capability 目录，单纯把截图保存到本机并在文字中写路径，模型仍无权读取；
- Tk 原生剪贴板不能可靠读取 Windows DIB 图片，当前环境已安装 Pillow，可使用 `ImageGrab.grabclipboard()` 读取并统一转存 PNG；
- `RuntimeTurn` 需要支持多个额外只读目录，Claude Code 命令才能为每个附件隔离目录追加 `--add-dir`。

## 决策记录

- 新增无密钥 `WorkspaceConfigStore/Service`，原子保存到应用数据目录；
- 系统配置新增“项目路径”页签，保存回调让新任务立即使用新路径；
- 保留 Incident `page_hint` 公共接口兼容 CLI/既有调用，但桌面端不再使用独立栏；
- 截图在 UI 层转成 PNG，附件引用通过 Incident API 显式传递，不从任意消息文本中猜路径；
- Runtime `additional_dirs` 只接受 Engine 校验后的附件父目录；Capability 目录继续独立挂载；
- 图片是证据而非指令，系统 Prompt 明确忽略截图中的命令式文本；
- 附件原文件先保留用于会话恢复和审计，本迭代不自动删除。

## 影响面与回退

- 影响：系统配置、桌面 composer、异常应用/Engine、共享消息与 RuntimeTurn、Claude Code 参数、附件存储、测试和文档；
- 不影响：开发修改/验证授权、SQL 只读策略、CLI 的 `--workspace/--page` 向后兼容、Capability 领域隔离；
- 回退：发布前可丢弃本任务变更；发布后可切回不可变 tag `v0.5.4`。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-26 | 现有 UI、配置、Incident API 与 Runtime 挂载路径检查 | 已完成 | 需要端到端附件契约，不能只做界面粘贴 |
| 2026-08-26 | 聚焦测试：配置、附件、Runtime、Incident、桌面 UI | `50 passed` | 配置与截图链路行为符合契约 |
| 2026-08-26 | 完整非 live pytest | `139 passed` | 无现有功能回归 |
| 2026-08-26 | Ruff 与 `git diff --check` | 通过 | 无静态检查或补丁格式问题 |

## 交付物

- `src/autocoding_agent/workspace_config.py`
- `src/autocoding_agent/incident_attachments.py`
- `src/autocoding_agent/interfaces/system_settings_ui.py`
- `src/autocoding_agent/interfaces/desktop_ui.py`
- `src/autocoding_agent/incident/application.py`
- `src/autocoding_agent/incident/engine.py`
- `src/autocoding_agent/core/models.py`
- `src/autocoding_agent/adapters/claude_code.py`
- 相关自动化测试与项目文档

## 发布记录

- 发布版本：`v0.5.5`
- 发布策略：单一功能提交、不可变 annotated tag、向 `origin` 推送一次
- 上一个稳定回退点：`v0.5.4`
