# ACE-RUNTIME-019：异常页面映射优先与搜索阶段门控

## 1. 问题

“小米良率上传”异常首轮在尚未查询Menu时发起宽泛Glob，随后被有界搜索策略阻断，导致会话失败。
正确流程应先用页面标题查询数据库映射，取得名称和URL/类名候选后再定位源码。

## 2. 实现

- 异常首轮及页面精确查询0行后的轮次只开放Read；
- 当前cycle的成功page_lookup返回非空候选后开放Read/Glob/Grep；
- business_data成功后保持源码搜索能力；
- 动态系统提示明确当前搜索是锁定还是已获得候选；
- policy_blocked事件增加脱敏pattern/path/glob/type/output mode/head limit；
- 开发流程的原有源码搜索能力不变。

## 3. 验收

- 首轮工具为Read并要求page_lookup；
- 页面精确查询0行后不提前解锁；
- 模糊查询返回候选后可精确Glob源码；
- 真实模型首轮返回page_lookup且未调用Glob/Grep；
- 199项非live测试、Ruff、compileall和Git whitespace检查通过；
- 发布v0.7.6并推送GitHub。
