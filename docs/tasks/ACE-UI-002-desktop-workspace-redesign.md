# 任务记录：ACE-UI-002 桌面 AI 工程工作台优化

- 任务编号：`ACE-UI-002`
- 当前状态：`done`
- 下一步责任方：维护方进入异常流程通用 Event/Recovery 与自治 SQL 调查迭代
- 创建日期：2026-08-25
- 目标：在不改变 Agent 业务规则的前提下，建立适合开发与异常长任务的统一桌面视觉体系。

## 验收标准

- 开发/异常流程、项目知识、工作区、页面线索、审批与恢复功能保持可用；
- 左侧任务导航、中央对话和底部任务输入区层级清楚；
- 主次按钮、状态、焦点和禁用态可辨识且不只依赖颜色；
- 桌面 UI 聚焦测试、完整回归、Ruff 和真实窗口检查通过；
- 设计来源、提示词、令牌和实现经验进入项目文档；
- 形成 `0.4.1` 单一提交和不可变 tag，并推送 GitHub 一次。

## 当前状态与关键发现

- 已确认方向：Vercel Coding Agent 任务输入结构、Vercel Chatbot 对话留白、Fluent 2 桌面令牌；
- 已重构主题、任务导航、顶栏、消息层级、上下文输入、审批/恢复卡和圆角按钮；
- 首次测试发现 `RoundedButton._options` 覆盖 Tkinter 内部 `_options()`，已改名并通过 16 项 UI 测试；
- 当前未改变 AgentEngine、数据库权限、Session 或 Runtime 行为。
- 已启动原生 Tkinter 窗口并检查 1874×1278 截图，确认品牌、流程选择、对话、上下文输入、
  主次按钮和窄侧栏没有重叠或截断。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-25 | `pytest tests/test_desktop_ui.py -q` | 16 passed | 已确认 |
| 2026-08-25 | UI 文件 Ruff | passed | 已确认 |
| 2026-08-25 | 原生窗口截图检查 | 1874×1278，无重叠或截断 | 已确认 |
| 2026-08-25 | `pytest -m "not live" -q` | 113 passed | 已确认 |
| 2026-08-25 | `ruff check src tests` | passed | 已确认 |
| 2026-08-25 | `git diff --check` | passed，仅换行提示 | 已确认 |

## 交付物

- `src/autocoding_agent/interfaces/desktop_ui.py`
- `src/autocoding_agent/interfaces/system_settings_ui.py`
- `docs/UI_DESIGN_GUIDE.md`
- `docs/PROJECT_EXPERIENCE.md`
- `tests/test_desktop_ui.py`
