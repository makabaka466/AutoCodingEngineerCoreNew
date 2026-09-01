# ACE-RUNTIME-026：自适应行动与证据完整性门控

## 1. 状态

- 任务编号：`ACE-RUNTIME-026`
- 当前状态：`waiting_user_confirm`
- 创建时间：2026-09-01
- 下一步责任方：用户确认总体方案；确认后由 Codex 从 Phase 1 开始实施
- 短状态快照：当前双 Engine 已允许模型做大量语义判断，但异常流程仍存在明显的页面查询、
  源码搜索、业务查询顺序门控。本计划保留状态机、安全权限和执行预算，把具体调查顺序改为模型
  自适应选择，同时用宿主可验证的证据完整性门阻止遗漏步骤或无证据完成。
- 用户授权：本轮只制定计划，不修改现有运行逻辑；计划完成后按项目版本规则推送 GitHub。

## 2. 用户目标

把 ACE 从“按固定步骤执行”升级为：

```text
模型根据当前事实选择下一项最有价值的行动
                    +
宿主持续检查证据是否真实、完整、无未解释冲突
```

达到以下效果：

- 不强制每个异常都依次执行“页面 SQL → 源码 → 业务 SQL → 结论”；
- 用户已经提供精确文件路径时，可以直接核对文件，不必再查 Menu；
- 只靠代码就能确认的问题，可以不查询业务数据；
- 涉及生产记录、用户数据或环境差异时，不能跳过必要的数据证据；
- 模型可以灵活选择行动，但不能因为漏掉证据就提前完成；
- 失败、冲突和无法取得的证据必须明确展示，不能由模型静默忽略。

## 3. 当前问题与可复用基础

### 3.1 当前已经具备

- `AgentDecision` / `IncidentDecision` 结构化模型决策；
- `AgentStateMachine` 负责生命周期与合法转换；
- `RuntimeLifecycle` 负责 Run、活动、工具和终态审计；
- Event Store、Decision Record、Artifact、QueryObservation 和 Recovery；
- Read/Glob/Grep、SQL、Hermes、修改和验证的宿主权限边界；
- 页面查询与业务查询独立预算、SQL 只读限制、源码搜索范围限制；
- 异常最终输出中的结论、原因、解决方法和置信度。

### 3.2 当前仍偏固定的部分

- `_source_search_enabled()` 主要根据页面查询结果决定是否开放源码搜索；
- `IncidentDecision.query_stage` 把数据库动作固定分成 page_lookup / business_data；
- `_validate_decision()` 强制业务查询和完成前必须存在 `LocatedPage.source_paths`；
- Prompt 对“先查页面、再读源码、再查业务”的顺序描述较强；
- `status` 同时承担“下一行动”和“本轮结果”两种职责；
- 当前没有统一 Evidence Ledger，代码证据、数据库观察、截图、Artifact 和用户陈述分散在不同字段；
- 完成校验主要检查字段是否存在，尚未检查结论中的关键主张是否引用真实证据。

## 4. 核心原则

### 4.1 不固定的内容

- 不固定页面定位一定通过数据库、用户输入还是精确文件路径完成；
- 不固定源码阅读、截图分析、知识检索、SQL 查询和 Hermes 咨询的先后顺序；
- 不固定必须使用多少次模型、多少个文件或多少条 SQL；
- 不要求所有任务都查询数据库或调用 Hermes；
- 不用关键词分数替代模型对“下一步最有价值行动”的判断。

### 4.2 必须固定的内容

- 工具权限、工作区边界、数据库只读、参数化、超时、限行和脱敏；
- 修改和验证前的用户审批；
- Runtime Run、Event、Artifact 和 QueryObservation 的可审计记录；
- 每种最终结果必须满足的证据类别；
- 结论主张必须引用当前 cycle 中真实存在的证据 ID；
- 冲突证据必须解决或明确列为未解决；
- 证据不足时只能继续调查、追问或诚实停止，不能伪装成已确认结论；
- 模型轮次、搜索、SQL、Hermes 和纠错仍有宿主硬上限。

状态机继续固定“任务处于什么生命周期”，但不再表示“下一步必须做什么调查动作”。

## 5. 目标架构

```text
用户输入 / 当前 Session
          ↓
ContextAssembler（只提供有界上下文）
          ↓
主模型：选择下一行动或提出最终结论
          ↓
EvidenceLedger：汇总宿主实际观察到的证据
          ↓
EvidenceGate：校验证据覆盖、引用、冲突和权限
          ↓
   ┌──────┼───────────────┐
   ↓      ↓               ↓
执行行动  要求补证据       接受最终结果
   ↓      ↓               ↓
Read/SQL/Hermes/Ask/Approval/Event/Artifact
   └──────────────→ 回到主模型
```

两个层次需要明确区分：

1. **Runtime 内微行动**：模型在当前权限内自行选择 Read/Glob/Grep 等工具；
2. **跨轮宿主行动**：模型通过结构化结果请求 SQL、Hermes、用户输入、审批或完成。

不要求模型把每一次 Read 都输出成 JSON Action，避免增加 Token 和框架复杂度。宿主通过现有
Runtime Activity 自动把实际工具结果投影为 Evidence Ledger 项。

## 6. 统一证据模型

计划新增：

```text
core/evidence/
├── models.py       EvidenceItem / EvidenceClaim / EvidenceGap / EvidenceAssessment
├── ledger.py       从 Event、Artifact、QueryObservation 等事实生成证据账本
├── policy.py       分领域声明证据完整性要求
└── gate.py         校验引用、覆盖、冲突和完成条件
```

### 6.1 `EvidenceItem`

建议字段：

```text
id                  稳定 ID
task_id / cycle     所属任务与轮次
kind                证据类别
source_type         user / screenshot / source / database / runtime / test / artifact / knowledge
source_ref          Event、Artifact、QueryObservation 或相对文件位置
summary             脱敏后的可读事实摘要
verification        observed / corroborated / unverified
freshness           current_cycle / historical
created_at
```

证据正文不重复保存：

- 数据库原始行仍不落库，只引用 QueryObservation 和查询 Artifact；
- 源码正文不复制进 Event Store，只引用 Runtime Activity、相对路径和必要行号；
- Screenshot 只引用受控附件和模型可见事实；
- RAG/Hermes 只能标记为 `knowledge` 候选证据，不能单独证明当前项目事实；
- 模型推理属于 `EvidenceClaim`，不允许伪装成 `EvidenceItem`。

### 6.2 `EvidenceClaim`

模型的关键主张必须显式关联证据：

```text
claim_id
claim_type          target / current_behavior / root_cause / solution / verification
statement
supporting_evidence_ids
certainty           confirmed / probable / possible
counter_evidence_ids
```

宿主不判断业务语义是否正确，但确定性检查：

- 引用 ID 是否真实存在；
- 是否属于当前 task/cycle；
- 是否引用了被策略允许的来源类型；
- 是否把历史知识误当作当前事实；
- 是否存在未解释的 counter evidence；
- `confirmed` 是否只有模型推断、没有任何宿主观察事实支撑。

### 6.3 `EvidenceAssessment`

```text
ready_for_outcome
coverage             已满足的证据类别
missing              缺少的证据类别及原因
conflicts            未解释冲突
declared_gaps         已明确但当前无法取得的证据
permitted_actions     当前权限和证据下可选择的宿主行动
recommended_focus     返回模型的短提示，不替模型决定具体行动
```

不采用总分 80 分之类的固定数值判断。Evidence Gate 只计算类别覆盖和引用完整性，语义上的下一行动
仍由模型选择。

## 7. 分领域证据完整性

### 7.1 异常处理最低完整性

完成一次页面相关异常调查前至少要有：

| 证据类别 | 要求 | 可以来自 |
| --- | --- | --- |
| 问题上下文 | 必须 | 用户描述、补充回答 |
| 目标身份 | 页面相关异常必须 | 精确路径、用户确认、Menu 候选并经源码核对、截图标题并经核对 |
| 当前行为 | 必须 | 页面/服务/数据访问代码、截图可见事实、运行日志 |
| 因果链 | 必须 | 至少一个宿主观察事实支撑“机制 → 症状” |
| 解决方法 | 必须 | 必须对应因果主张，不能只给通用建议 |
| 证据缺口 | 必须声明 | 无法访问的生产日志、环境或外部接口 |

条件证据：

- 有截图：必须生成视觉观察，或明确说明截图不可读；
- 结论声称某条生产/用户记录异常：必须有业务数据库或日志证据；
- 结论声称外部接口返回异常：必须有接口响应/日志证据，否则只能标记 probable；
- 只从代码即可证明机制时：可以不查业务 SQL，但必须说明“证明的是代码机制，不是生产触发事实”；
- 页面与 Menu 候选、截图或源码不一致：冲突未解决前不能给 confirmed 结论。

页面定位不再固定要求先查 Menu：

- 用户给出精确存在的相对路径，可以直接 Read 验证；
- 用户只给页面标题，模型通常选择 Menu 查询；
- 用户给出截图但标题不清，模型通常选择追问；
- 多个入口都可以形成 `target_identity`，但都必须留下来源和核对证据。

### 7.2 开发流程最低完整性

| 任务类型 | 完成前需要的证据 |
| --- | --- |
| 解释/调查 | 用户目标、目标代码、当前行为、证据支持的回答 |
| 修改方案 | 上述证据 + before/after 方案 + 影响与验证计划 |
| 已实施修改 | 修改审批 + 基线 + Patch/changed files + 方案对应关系 |
| 已验证修改 | 测试审批 + TestExecuted/构建结果 + 失败说明 |

若代码已修改但验证尚未授权，模型应优先请求 verify 审批；如果用户明确不验证，最终结果必须把
“未验证”列为 EvidenceGap，不能写成“已经修复并验证”。

## 8. 自适应行动契约

计划把“模型结果”和“下一宿主行动”分开：

```text
AgentActionKind
- ask_user
- query_database
- consult_hermes
- request_modify_approval
- request_verify_approval
- complete
- stop_with_gaps
```

源码 Read/Glob/Grep 仍是 Runtime 内工具，不单独成为跨轮 Action。

结构化决策新增：

```text
requested_action
claims
known_gaps
resolved_conflicts
why_this_action
```

`why_this_action` 只要求简短、可审计的行动理由，不要求或保存模型思维链。

宿主处理算法：

```python
while budget_available:
    evidence = ledger.snapshot(session)
    permitted = action_policy.allowed(evidence, permission, budget)
    decision = model.choose_next_action(context, evidence.compact(), permitted)
    validate_schema_and_references(decision, evidence)

    if decision.requested_action == COMPLETE:
        assessment = evidence_gate.assess(decision, evidence)
        if assessment.ready_for_outcome:
            persist_completion()
            break
        return_missing_evidence_to_same_model(assessment)
        continue

    execute_host_action(decision.requested_action)
    project_new_facts_into_ledger()
```

达到预算上限时不得自动宣称完成：

- 缺少用户才能提供的信息：返回 `needs_input`；
- 外部证据不可访问但已有概率性解释：返回带 EvidenceGap 的保守结论；
- 没有足够因果证据：返回未完成/失败，并说明缺口和下一步。

## 9. 与状态机、Event 和 Recovery 的关系

### 9.1 状态机继续负责

- inspecting / querying_data / waiting_input；
- waiting_modify_approval / implementing；
- waiting_verify_approval / verifying；
- paused / recovery_required / completed / failed / cancelled；
- 合法转换、版本、幂等命令和恢复入口。

不为“正在读页面”“正在看截图”“正在查日志”新增持久状态；这些仍是 ProgressEvent 和 EvidenceItem。

### 9.2 新增审计事件

- `EVIDENCE_OBSERVED`：宿主从工具/查询/附件得到新证据；
- `EVIDENCE_ASSESSED`：记录覆盖类别、缺口和冲突，不保存原始业务行；
- `COMPLETION_BLOCKED`：模型尝试完成但证据门未通过；
- `ACTION_SELECTED`：记录结构化下一行动及简短理由；
- `EVIDENCE_CONFLICTED` / `EVIDENCE_GAP_DECLARED`：记录冲突与无法取得的证据。

### 9.3 Recovery

恢复时从 Event/Artifact/QueryObservation 重建 Evidence Ledger，不重新执行已经完成的 SQL、Hermes、
修改或测试。中断在只读行动时回到 paused；中断在可能有副作用的行动时仍进入 recovery_required。

## 10. 分阶段实施计划

### Phase 0：基线与评测集

目标：先固定当前行为和真实场景，避免“更灵活”变成不可测。

- 建立异常/开发各不少于 8 个代表场景；
- 记录当前 Runtime 次数、SQL 批次、Token、最终结论和证据缺陷；
- 标注每个场景的必需证据与可跳过证据；
- 不改生产流程。

交付：`tests/evals/evidence_cases/` 或等价的离线 Fixture、基线报告。

### Phase 1：Evidence Model 与 Ledger（旁路）

目标：增加统一证据模型，但不影响当前结果。

- 新增 `core/evidence/`；
- 从 Event、Runtime Activity、QueryObservation、Artifact、附件和用户消息投影证据；
- 不复制原始数据库行和完整源码；
- Event Store 保存证据引用和摘要；
- 旧 Session 可在读取时按现有记录重建，不强制迁移。

验收：现有流程输出完全不变；Evidence Ledger 可解释“证据从哪里来”。

### Phase 2：Evidence Gate 影子评估

目标：先观察门控判断，不立即阻止模型完成。

- Incident/Development 定义独立 Evidence Profile；
- 每次模型完成时执行 shadow assessment；
- 记录 missing/conflict，但仍保持旧结果；
- 用现有真实异常记录校准条件证据，不使用总分阈值。

验收：能够发现“有结论无数据”“有修改无验证”“页面候选冲突未解决”等缺陷，误报有清单。

### Phase 3：异常流程自适应行动

目标：优先改造最重要的异常流程。

- 引入跨轮 `AgentActionKind`；
- 把页面 SQL 前置改为 target_identity 证据门；
- 精确用户路径经 Read 验证后可跳过 Menu；
- 保留 SQL/源码搜索/附件的安全策略和预算；
- Completion 未通过证据门时，允许同一模型获得一次“缺失证据”纠正；
- 无法补齐时返回 needs_input 或带明确缺口的保守结果。

验收：章节 12 的异常场景全部通过，并完成一次真实只读 MES 流程验收。

### Phase 4：开发流程自适应行动

目标：让开发流程同样按证据选择调查、方案、修改和验证。

- 解释型任务不被迫走修改流程；
- 有精确文件时直接阅读，信息不足时先追问；
- Proposal 必须引用当前代码证据；
- 修改完成必须有 Patch Artifact；
- 验证结果或明确 EvidenceGap 进入最终报告；
- 审批与副作用恢复规则不变。

### Phase 5：UI、审计与恢复展示

- 状态条展示模型选择后的宿主动作，如“正在核对页面证据”；
- 结果卡展示“已确认事实 / 推断 / 证据缺口 / 解决方法”；
- 提供“为什么得出这个结论”的证据引用，而不是思维链；
- Recovery 页面展示中断动作和已获得证据，避免重复执行。

### Phase 6：Token 与可靠性优化

- 每轮只注入 Evidence Ledger 的紧凑摘要和最近新增证据；
- 稳定证据通过 ID 引用，不重复注入全文；
- 相同 EvidenceItem 去重；
- 建立 unsupported claim rate、missing evidence rate、平均模型轮次和 Token 指标；
- 默认自适应流程稳定后再删除旧固定门控兼容代码。

## 11. 迁移策略

- Phase 1/2 只旁路记录，不改变用户结果；
- Phase 3 先通过配置开关只启用异常流程；
- 保留旧 `IncidentDecision` 读取兼容，新字段提供默认值；
- 不直接删除 `query_stage`，先降级为预算/审计用途，确认无依赖后再评估；
- Evidence Ledger 从事实来源重建，不把它作为第二份业务真相数据库；
- 每个 Phase 独立版本、测试、提交、Tag 和 GitHub 推送，可单独回退；
- 旧 Session 继续可读，活动任务不在中途切换执行策略。

## 12. 核心验收场景

### 12.1 异常处理

1. **精确路径**：用户给出页面相对路径，ACE 直接读取并核对，可以跳过 Menu 查询；
2. **只有页面标题**：模型选择 Menu 查询，再验证候选源码；
3. **截图无标题**：模型聚焦截图后追问，不进行全表或全仓搜索；
4. **代码机制足够**：不执行业务 SQL，也能输出“代码机制已确认、生产触发待验证”的结论；
5. **记录级异常**：没有对应业务数据证据时，Completion 被阻止；
6. **页面冲突**：Menu、截图和源码候选不一致时，先核对或询问，不能静默选一个；
7. **查询失败**：模型可以改查日志、代码或询问，不能固定重跑同一 SQL；
8. **预算耗尽**：输出已确认事实和缺口，不伪造根因；
9. **简单续聊**：已有证据足够时继续使用紧凑路由，不重新调查；
10. **新症状续聊**：升级完整调查，但复用仍有效的证据引用。

### 12.2 开发流程

1. 精确文件修改任务直接读目标文件并给方案；
2. 文件/目标不清时只问一个高价值问题；
3. 修改方案没有代码证据时不能申请审批；
4. 未获修改审批时不能写文件；
5. 修改完成没有 Patch Artifact 时不能宣称已修改；
6. 测试失败后可选择重新调查或新方案，不固定只能重跑；
7. 未验证时最终报告明确展示验证缺口；
8. Recovery 后不重复可能已产生副作用的动作。

## 13. 验收标准

- 模型可以基于当前 Evidence Snapshot 选择下一宿主行动；
- 状态机只负责生命周期，不编码业务调查顺序；
- Completion 必须通过分领域 Evidence Gate；
- 每个关键 Claim 都能追溯到当前 cycle 的 Evidence ID；
- 精确路径场景不再强制 Menu SQL，记录级异常仍不能跳过必要数据证据；
- RAG/Hermes 不得单独把 Claim 提升为 confirmed；
- 原始业务行、源码正文和模型思维链不进入 Evidence Event；
- 数据库只读、搜索边界、审批、预算、Event/Recovery 全部保持；
- 旧 Session 可读取，活动 Session 不被静默迁移；
- 全部非 live 测试通过，并增加影子评估、证据缺失、冲突、恢复和真实只读验收；
- 每个 Phase 更新架构、接口、工程经验，形成一个可回退版本并推送 GitHub。

## 14. 风险与控制

| 风险 | 控制 |
| --- | --- |
| 模型自由选择导致漏步骤 | Completion 由 Evidence Gate 拦截，不依赖模型自觉 |
| Evidence Gate 再次变成写死流程 | 只校验证据类别、引用和权限，不规定获得顺序与工具 |
| 模型把推理包装成证据 | EvidenceItem 只能由宿主从实际事件/产物投影 |
| 为了补证据无限循环 | 模型、SQL、搜索、Hermes 和纠错均保留硬预算 |
| 证据摘要增加 Token | 使用 ID、增量摘要、去重和稳定前缀，不注入原始全文 |
| 页面规则放宽导致全库搜索 | Source Search Policy 与工作区边界保持独立硬约束 |
| 旧会话无法继续 | 新字段有默认值，Ledger 可从旧 Event/Observation 重建 |
| 低置信度结论被误读为事实 | 输出明确区分 confirmed / probable / possible 和 EvidenceGap |

## 15. 相关文件

- `src/autocoding_agent/core/models.py`
- `src/autocoding_agent/incident/models.py`
- `src/autocoding_agent/core/engine.py`
- `src/autocoding_agent/incident/engine.py`
- `src/autocoding_agent/core/runtime_lifecycle.py`
- `src/autocoding_agent/core/state_machine/`
- `src/autocoding_agent/core/audit/`
- `src/autocoding_agent/core/artifacts/`
- `src/autocoding_agent/database_models.py`
- `src/autocoding_agent/core/search_policy.py`
- `src/autocoding_agent/incident/prompts/incident_workflow.md`
- `tests/test_agent_flows.py`
- `tests/test_incident_flow.py`
- `tests/test_recovery.py`

## 16. 发现与决策

- 已确认：当前问题不是状态机本身，而是部分业务顺序同时存在于 Prompt、工具门控和校验代码中；
- 已确认：完全放开模型会有漏证据风险，完全固定流程会造成无效查询和适应性不足；
- 决策：采用“模型选择行动 + 宿主验证证据”的混合架构；
- 决策：EvidenceItem 只能由宿主观察生成，模型只能提出 Claim 和引用；
- 决策：不使用统一数值分数决定完成，采用分领域证据类别和条件要求；
- 决策：先异常、后开发，先 shadow、后 enforcement；
- 决策：状态机、权限、预算、审批、Event/Recovery 均保留；
- 决策：本轮仅交付计划，不宣称自适应 Evidence Loop 已实现。

## 17. 阻塞与验证

- 阻塞：等待用户确认是否按 Phase 0 → 1 → 2 → 3 的顺序先实施异常流程；
- 已验证：对照当前 `IncidentDecision`、`AgentDecision`、`_validate_decision()`、
  `_source_search_enabled()`、StateMachine、Event/QueryObservation 和现有测试完成设计映射；
- 未验证：Evidence Profile 的误报率、真实 Token 变化和实际 MES 场景效果，必须在 shadow 阶段评估。

## 18. 交付物与待确认

- 交付物：本任务卡；
- 待确认：优先实施异常流程，开发流程在异常 Evidence Gate 稳定后接入；
- 待确认：允许输出 `probable + EvidenceGap` 的保守完成，还是证据缺口一律保持 needs_input；
- 待确认：Phase 3 默认先通过配置开关启用，完成真实只读验收后再替换旧流程。
