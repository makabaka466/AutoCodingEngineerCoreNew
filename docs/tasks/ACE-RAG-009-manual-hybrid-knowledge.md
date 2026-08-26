# 任务记录：ACE-RAG-009 手动混合检索知识库

- 任务编号：`ACE-RAG-009`
- 当前状态：`completed`
- 下一步责任方：用户完成 Ollama/Embedding 部署后，另开迭代接入正式 Adapter 并全量重建
- 创建日期：2026-08-26
- 用户授权：按已确认方案实现 RAG；任务完成后的 MD 由用户在管理页面手动选择加入知识库。
- 范围修订：用户正在部署 Ollama 与 `Qwen3-Embedding-0.6B`，本迭代先提供可替换协议和明确标识的确定性伪实现，不连接真实 Ollama/Qdrant，不把伪向量标记为正式 Qwen 索引。
- 目标版本：`v0.6.0`
- 基线与回退点：`v0.5.5` / commit `15a5f29`

## 目标

在不改变 Markdown 原文身份的前提下，建立“文档发现 → 人工选择 → Markdown 分块 →
Embedding → 向量/FTS5 双索引 → RRF 混合检索 → Agent 引用”的本地 RAG 骨架。

## 验收标准

- 提供 `EmbeddingProvider` 与 `VectorStore` 稳定协议，真实 Ollama/Qdrant 可在后续替换；
- 当前使用可重复、无网络的 Fake Embedding 和持久化 Fake Vector Store，并在 UI/元数据中明确标识；
- 发现 Project Knowledge、自动 Capability 和项目工程经验 Markdown，但不自动索引；
- 管理页面展示待加入、已索引、已更新、失败和已移除状态；支持多选加入、重建、移除、分块预览和测试检索；
- Markdown 按标题、段落、代码块切分，保存标题路径、内容 Hash、来源、领域和项目元数据；
- 每个 Chunk 同时进入向量索引与 SQLite FTS5；查询使用 Dense Top-K、BM25 Top-K 和 RRF 融合；
- 只有手动加入的文档可被检索；移除索引不删除源 MD；内容变化后显示待重建；
- 开发和异常只在只读调查阶段查询 RAG，检索失败不得导致任务失败；
- 注入模型的知识带来源引用并被标记为不可信、可能过期，必须结合当前代码/数据库验证；
- 完整非 live 测试、Ruff、编译和 diff 检查通过；
- 更新 README、架构、接口和项目经验，单一提交/tag 推送到 GitHub。

## 已确认设计

- Markdown 与 SQLite Chunk 正文是知识原文；向量索引可以重建；
- 本迭代 Fake 模型 ID 固定为 `fake-hash-embedding-v1`，与未来
  `qwen3-embedding:0.6b` Collection/索引版本严格隔离；
- Chunk 采用 Markdown 标题感知切分，目标约 600–900 tokens、最大约 1200、同章节少量重叠；
- 关键词索引使用 SQLite FTS5；向量与关键词结果由宿主使用 RRF 按排名融合；
- 任务完成只生成 Capability MD，并在管理页显示为待加入，不触发自动索引；
- 删除知识索引只删除 Chunk/FTS/Vector 记录，不删除用户维护或自动生成的 Markdown；
- 真实 Ollama/Qdrant 部署完成后需要更换 Adapter 并明确执行全量重建，禁止混用伪向量。

## 影响范围与未授权范围

- 影响：知识模型、SQLite RAG 数据库、管理 UI、开发/异常只读 Prompt、测试和文档；
- 外部副作用：最终按用户既有要求发布一次 Git commit/tag 到 `origin`；
- 未授权：不启动或配置 Ollama/Qdrant、不下载模型、不上传企业文档到云服务、不自动索引新任务文档；
- 失败停止条件：持久化一致性、现有 139 项回归、Agent 权限边界或源文档安全任一项未满足则不发布。

## 验证记录

| 日期 | 验证 | 结果 | 结论 |
| --- | --- | --- | --- |
| 2026-08-26 | 当前 Capability/Project Knowledge、Engine 与桌面入口检查 | 已完成 | 可在现有 SQLite/Capability 边界外增加可重建检索层 |
| 2026-08-26 | RAG、开发、异常与桌面聚焦回归 | 59 passed | 注入、降级、人工索引、陈旧隔离和管理页行为符合契约 |
| 2026-08-26 | 完整非 live 回归 | 148 passed | 无既有功能回归 |
| 2026-08-26 | Ruff / compileall / git diff --check | 通过 | 代码、导入、语法与补丁格式通过发布检查 |

## 交付物

- `src/autocoding_agent/knowledge_rag/`
- `src/autocoding_agent/interfaces/knowledge_management_ui.py`
- 开发/异常 Engine 和组合根接入
- 桌面知识库入口
- 自动化测试、README、架构、接口与项目经验

## 发布记录

- 提交：`feat: add manual hybrid RAG knowledge framework`
- 标签：`v0.6.0`
- 推送目标：`origin/main` 与 `v0.6.0`（本任务发布步骤执行）
