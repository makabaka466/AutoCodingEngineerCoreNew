# 任务记录：ACE-KNOWLEDGE-007 会话级能力文档追加

- 任务编号：`ACE-KNOWLEDGE-007`
- 当前状态：`done`
- 下一步责任方：用户在桌面客户端验证真实开发与异常会话的续聊追加体验
- 创建日期：2026-08-25
- 用户授权：用户明确要求任务首次完成时生成新的 Markdown；同一会话完成后继续对话，后续完成结果追加到原文档，不再为每个 Cycle 新建文档。
- 目标：把能力知识的文件边界调整为“一次会话一份文档”，同时保留 Cycle 的状态审计、恢复和预算语义，并继续严格区分开发与异常处理知识。

## 验收标准

- 新开发会话首次完成，在 development 能力目录创建一份 Markdown；
- 新异常会话首次完成，在 incident 能力目录创建一份 Markdown；
- 同一 Session 重新打开并再次完成时，追加到原 Markdown，不创建 `cycle-002.md`；
- 后续轮次保留独立标题、目标、总结、证据、验证或诊断内容；
- task JSON 保存会话的轮次历史，并能对同一 Cycle 幂等重试；
- Capability 索引每个 Session 只出现一项，并显示累计完成轮次；
- `cycle_number`、`task_reopened`、查询/重规划预算重置和恢复逻辑保持不变；
- 开发与异常能力目录、索引和内容结构保持隔离；
- 已有 v0.5.3 逐轮 task 记录不会导致索引重复或相同 Cycle 再次追加；
- 自动化测试、完整非 live 回归、Ruff 和 diff 检查通过；
- 工程经验同步更新，形成下一版本提交/tag 并向 `origin` 推送一次。

## 当前状态与关键发现

- v0.5.3 已实现完成后续聊，但 Capability 身份是 `(session_id, cycle_number)`；
- 开发和异常 Store 都会创建 `<session-id>-cycle-002.md`，与最新业务规则冲突；
- Session 的 cycle 字段仍是执行、事件和恢复所需，不能因为文件合并而删除；
- 现有 `CapabilityReceipt.created` 可以继续表达“首次创建”与“更新原文档”，无需扩展公共接口。

## 决策记录

- Capability 文件和主 task JSON 使用稳定的 `session_id`；
- 首次完成写完整文档，后续完成以明确的“后续工作轮次”章节追加；
- 主 task JSON 升级为会话聚合记录，保存 `cycles`、`cycle_count` 和 `last_cycle_number`；
- 同一轮重复调用通过已记录的 cycle 编号返回 `created=false`，不得重复追加；
- 索引以 Session 去重，开发和异常继续使用各自独立目录；
- 兼容旧的 `session-id-cycle-NNN.json` 记录：至少用于幂等判断和索引去重，不删除历史文件。

## 影响面与回退

- 影响：开发/异常 Capability Store、索引、能力契约、相关测试与文档；
- 不影响：状态机、Runtime Session、事件、SQLite 恢复、SQL 权限和 Artifact 存储；
- 回退：发布前可丢弃本任务变更；发布后可切回不可变 tag `v0.5.3`。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-25 | 规则与现有实现差异检查 | 已完成 | 需要修改双 Capability Store、索引、测试和文档 |
| 2026-08-26 | 开发、异常、知识索引、状态机和旧记录兼容定向测试 | `51 passed` | 通过 |
| 2026-08-26 | 完整非 live 回归 | `129 passed` | 通过 |
| 2026-08-26 | `ruff check src tests` | `All checks passed` | 通过 |
| 2026-08-26 | `git diff --check` | 仅 Git 的 LF/CRLF 提示，无空白错误 | 通过 |

## 交付物

- `src/autocoding_agent/adapters/capability_store.py`
- `src/autocoding_agent/incident/capability_store.py`
- Capability 相关自动化测试
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/INTERFACES.md`
- `docs/PROJECT_EXPERIENCE.md`

## 发布记录

- 版本：`v0.5.4`
- 发布策略：单一功能提交、不可变 annotated tag、向 `origin` 推送一次
- 上一个稳定回退点：`v0.5.3`
