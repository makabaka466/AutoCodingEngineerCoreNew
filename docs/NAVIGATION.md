# 页面目录与路径映射（0.19.0）

系统配置 → MD 能力配置 → 入口定位 → 对应项目，维护一个 ace-navigation JSON 代码块。

```ace-navigation
{
  "catalogs": [{
    "datasource_id": "menu_default",
    "sql": "SELECT ID AS menu_id, NAME AS menu_name, URL AS menu_path FROM Menu",
    "parameters": {}, "page_size": 100, "max_rows": 500, "ttl_seconds": 300
  }],
  "path_mappings": [{
    "namespace_prefix": "Ckhy.MES.Client.CKClient",
    "relative_directory": "zwqtmes/MESClient/CKClient", "extension": ".cs"
  }]
}
```

示例表/列必须按项目实际 schema 确认。可指定 ds- 开头的数据源 ID；menu_default 只对应任务绑定的菜单默认库，没有默认值时不能猜库。SELECT 仅返回稳定唯一 menu_id、menu_name、menu_path，不包含用户关键词、ORDER BY 或分号。Oracle 联表亦用这些别名，固定语言/应用条件用参数绑定。

模型通过 query_required 中唯一一项 DataQuery 请求 query_kind=menu_catalog，省略 SQL/parameters；异常流程使用 page_lookup。宿主按 menu_id 排序分页，不增加分页模型调用。每页最多100行，默认总500行、最高2000行，最多25页，目录文本不超过120k字符。超过上限、重复ID、截断或字段异常均明确标记不完整，不缓存为完整目录。

完整缓存默认300秒；按项目、根目录、绑定源/环境/连接指纹和配置隔离。命中前仍检查当前授权与配置。refresh_catalog=true 强制刷新；失败不回退旧缓存。缓存只存菜单导航三字段，不存业务查询结果。分页读完不等同于事务一致快照，菜单并发变化仍需当前源码核对。

路径解析只使用相对路径或显式命名空间映射；只返回实际存在且位于工作区内的文件。不递归全仓、不按近似名称挑文件。多个规则命中不同文件时全部保留为候选并报告冲突。缺失文件、符号链接越界等不伪造路径。

目录只是候选线索。模型比较名称、路径、用户描述与图片，再读取源码确认身份；目录查询不得用于根因确认。摘要保存来源、时间、缓存与完整性，原菜单行只进入当前模型调用和目录缓存。停止在分页边界生效；未完整读取的只读目录可在恢复后重读，不重放代码修改。

本次本地生物/车载模板已写入入口 MD，并备份至 outputs/stage4-knowledge-backup。尚未连接真实库；需先绑定数据源、选择菜单默认库并核验模板列名/语言/应用条件。
