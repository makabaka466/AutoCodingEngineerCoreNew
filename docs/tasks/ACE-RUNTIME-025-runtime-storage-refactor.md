# ACE-RUNTIME-025：Runtime 与 SQLite 共用能力收敛

## 1. 状态

- 状态：`done`
- 开始时间：2026-09-01
- 完成时间：2026-09-01
- 下一步责任方：用户验收；如需继续瘦身，另建“可选入口/兼容层删除”任务并先确认产品边界
- 用户授权：重构开发/异常 Engine 和 SQLite Store，补充主流程功能注释；保持行为与安全边界，
  完成后更新工程经验并按一次迭代一个版本推送 GitHub。

## 2. 目标

- 把双 Engine 重复的 Runtime Run、活动审计、进度投影、Usage 合并和 RAG 检索提取为独立方法；
- 把双 SQLite Store 重复的连接、事件、Run、Command、状态回放等功能收敛到共用类；
- Engine 保留开发审批和异常调查等领域编排，不引入复杂继承框架；
- 在主入口和循环中添加编号式流程注释，使维护者能看懂每一步为何存在；
- 不改变数据库只读、工具权限、状态恢复、事件审计和历史会话兼容行为。

## 3. 验收标准

- 开发和异常 Engine 调用同一个 Runtime 生命周期与知识检索组件；
- Task/Incident SQLite Store 调用同一个连接和追加式 Runtime 持久化组件；
- 原 211 个非 live 测试全部通过，并增加共用组件直接测试；
- Ruff、compileall、diff 检查通过；
- `docs/ARCHITECTURE.md`、`docs/INTERFACES.md` 和 `docs/PROJECT_EXPERIENCE.md` 同步；
- 形成单一版本提交、Tag，并推送 `origin/main`。

## 4. 关键发现与决策

- 已确认：代码体积不大，但两个 Engine、两个 SQLite Store 存在明显机械逻辑重复；
- 已确认：领域流程不能合并为一个万能 Engine，否则会重新增加框架复杂度；
- 决策：优先使用小型组合组件和普通方法，不使用 Engine 继承层次；
- 决策：SQLite 共用类支持既有开发/异常表布局，不在本次强制迁移生产数据表结构；
- 决策：`docs/ERROR_LOG.md` 是用户未跟踪文件，本次不修改、不提交。

## 5. 尝试、验证与交付物

- 新增 `core/runtime_lifecycle.py`，统一 Run、Runtime Activity、测试命令审计、进度投影和 Usage；
- 新增 `core/knowledge_context.py`，统一双流程 RAG 检索和失败降级事件；
- 新增 `adapters/sqlite_runtime.py`，统一 SQLite 连接、UUID、Event/Run/Command 和状态回放；
- `AgentEngine`、`IncidentEngine` 保留领域分支并增加编号式主流程说明；
- `SQLiteTaskStore`、`SQLiteIncidentStore` 保留领域 snapshot/schema/迁移并组合共享数据库类；
- 四个原重复文件由约 4580 行降至约 3880 行；新增共享实现约 756 行，职责来源从两份变为一份；
- 新增 `tests/test_runtime_support.py`，直接覆盖共享 Runtime 与 SQLite 基础设施；
- 定向验证：66 passed；全量非 live 回归：214 passed；
- 文档：`ARCHITECTURE.md`、`INTERFACES.md`、`PROJECT_EXPERIENCE.md` 已同步到 0.7.12；
- 阻塞：无；
- 回退方式：发布后的 `v0.7.12` 可完整回退，本轮之前的稳定点为 `v0.7.11`。
