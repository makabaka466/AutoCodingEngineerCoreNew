# 任务记录：ACE-RUNTIME-006 已完成会话续聊与分轮能力沉淀

- 任务编号：`ACE-RUNTIME-006`
- 当前状态：`done`
- 下一步责任方：用户在桌面客户端按真实任务验证完成后续聊体验
- 创建日期：2026-08-25
- 用户授权：用户确认 `COMPLETED` 只结束当前工作轮次；同一会话继续发送时应重新进入分析，
  且每个再次完成的工作轮次生成独立 Markdown，不与旧能力文档混写。
- 目标：让开发和异常会话在完成后仍可继续追问或追加需求，同时保留可审计状态转换、独立
  执行预算和不可覆盖的逐轮能力文档。

## 验收标准

- `COMPLETED` 会话允许新的 `SUBMIT_USER_INPUT`，发送后转换到 `INSPECTING`；
- `CANCELLED`、`PAUSED` 和 `RECOVERY_REQUIRED` 的现有边界不被放宽；
- 同一会话保留消息、Runtime Session、Event、Decision、Run 和 Artifact 历史；
- 新工作轮次清除临时状态，并重置数据库查询次数和开发重规划次数；
- 每次从一个工作轮次再次到达 `COMPLETED`，生成一份独立、不可覆盖的 Capability Markdown；
- 中间的澄清、查询、审批和验证不单独生成 Capability；
- 桌面端已完成会话的输入框与发送按钮可用，并明确提示“继续对话”；
- SQLite 保存和事件回放可识别 `COMPLETED → INSPECTING → COMPLETED`；
- 双流程、Capability、UI、状态机和持久化测试通过；
- 工程经验同步更新，形成 `v0.5.3` 提交/tag 并向 `origin` 推送一次。

## 当前状态与关键发现

- 状态机当前把 `COMPLETED` 定义为无出边状态，开发与异常 Engine 也分别拒绝继续发送；
- 桌面 UI 根据 `COMPLETED` 直接禁用 composer，因此问题不是单一界面缺陷；
- Artifact 使用 UUID 文件名，天然可以在同一 Session 中继续追加；
- Capability 当前以 Session ID 作为唯一文件和任务记录名，第二次完成会被当作重复写入忽略；
- 查询和重规划计数当前属于整个 Session，若直接开放续聊会错误继承上一轮预算。

## 决策记录

- `COMPLETED` 保持当前轮次的静止完成态，只有新用户消息才能显式重新打开；
- 状态机新增 `COMPLETED → INSPECTING`，Recovery 仍把静止的完成态视为无需扫描；
- 新增持久化 `cycle_number` 和当前轮次目标字段，旧 Session 默认视为第 1 轮；
- 第 1 轮 Capability 保留既有文件名，后续采用 `session-id-cycle-NNN.md`，避免迁移旧文件；
- 每轮文档独立写入并进入索引，不修改或追加旧文档；
- `CANCELLED` 仍是永久封闭状态，不能通过普通消息重新打开。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-25 | 状态机、双 Engine、UI、Capability 与 SQLite 路径检查 | 已完成 | 已确认 |
| 2026-08-25 | 定向测试：状态机、双流程、SQLite、Artifact、桌面 UI | `78 passed` | 通过 |
| 2026-08-25 | 完整非 live 回归 | `128 passed` | 通过 |
| 2026-08-25 | `ruff check src tests` | `All checks passed` | 通过 |
| 2026-08-25 | `git diff --check` | 仅 Git 的 LF/CRLF 提示，无空白错误 | 通过 |

## 影响面与回退

- 影响：共享状态机、开发和异常 Session/Engine、Capability Store、Artifact 元数据、桌面控制状态；
- 不影响：审批权限、SQL 只读限制、Runtime 工具权限、取消态封闭和历史产物不可变性；
- 回退：发布前可丢弃本任务文件变更；发布后可切回不可变 tag `v0.5.2`。

## 交付物

- `src/autocoding_agent/core/state_machine/machine.py`
- `src/autocoding_agent/core/models.py`
- `src/autocoding_agent/core/engine.py`
- `src/autocoding_agent/incident/models.py`
- `src/autocoding_agent/incident/engine.py`
- `src/autocoding_agent/adapters/capability_store.py`
- `src/autocoding_agent/incident/capability_store.py`
- `src/autocoding_agent/interfaces/desktop_ui.py`
- `src/autocoding_agent/interfaces/streamlit_ui.py`
- 相关自动化测试
- `docs/PROJECT_EXPERIENCE.md`

## 发布记录

- 版本：`v0.5.3`
- 发布策略：单一功能提交、不可变 annotated tag、向 `origin` 推送一次
- 上一个稳定回退点：`v0.5.2`
