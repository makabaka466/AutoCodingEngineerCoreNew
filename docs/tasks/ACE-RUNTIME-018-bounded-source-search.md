# ACE-RUNTIME-018：恢复有界 Glob/Grep 源码定位

## 1. 目标

恢复 Claude Code 原生 `Glob/Grep`，让开发与异常流程能根据数据库映射、类名、页面标题、路由或
相对路径在配置工作区内自行定位嵌套源码；同时禁止把全库枚举当作默认调查方式。

## 2. 已确认根因

- Runtime 的策略层声明了 `Read/Glob/Grep`；
- Claude Code 命令同时使用 `--bare`，实际会移除 Glob/Grep；
- 目标页面真实存在于工作区下的嵌套 `zwqtmes` 目录；
- 数据库返回的是可推导精确文件名的完整类名，但模型只能猜测路径并连续 Read；
- `--disallowedTools Glob(**/*)`对当前Claude Code 2.1.237的实测没有拦截效果。

## 3. 实现范围

- Runtime 使用 `--safe-mode`，保持空 setting sources、严格空 MCP、无Chrome和精确工具白名单；
- 开发/异常共享有界搜索提示，要求“已知路径直接Read、精确文件名Glob、候选子树Grep”；
- 新增 `BoundedSearchGuard`：8次组合预算、Glob广度检查、Grep 1..100输出限制、目录文件过滤、
  workspace/授权目录边界；
- 违规流式调用生成 `policy_blocked` 活动并停止当前Runtime；
- 工具审计增加脱敏后的 pattern/glob/type/output mode/head limit；
- 同步架构、接口和项目工程经验文档。

## 4. 验收

- `**/FCModelUpload.cs`可定位嵌套源码；
- `**/*`和`**/*.cs`被拒绝；
- 无`head_limit`或无文件范围的目录Grep被拒绝；
- 超出workspace/授权只读目录的搜索被拒绝；
- 第9次搜索被拒绝并有审计事件；
- 全量非live测试、Ruff、compileall和Git whitespace检查通过；
- 发布`v0.7.5`并向GitHub推送一次。
