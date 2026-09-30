# HTTP API：独立接口与可靠执行（0.10.0）

`src/autocoding_api/` 是独立交付层，HTTP 和执行进程分别运行；二者只调用现有
`AgentApplication` / `IncidentApplication`。任务首先持久化到
`<data_dir>/api/queue.sqlite3`，领域会话仍保存到既有 runtime SQLite。API 进程只读领域会话，不构建
Runtime 或执行恢复扫描。当前版本支持创建开发与异常诊断任务、追加普通消息、查询任务/操作，
以及显式只读恢复；不开放代码修改/验证审批接口。需要审批的开发任务会停在原有等待审批状态。

## 本机启动

以下示例使用 Windows PowerShell；把路径换成实际位置。第一次安装依赖时在仓库根目录运行：

```powershell
D:\python\python.exe -m pip install -e '.[api]'
Copy-Item deploy\api\server.example.json deploy\api\server.local.json
D:\python\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

编辑 `deploy\api\server.local.json`：使用实际存在的绝对项目目录，将命令生成的随机 Token
填入 `token`，并设置独立、持久的 `data_dir`。`server.local.json` 已被 Git 忽略；限制此文件和
`data_dir` 仅供服务账户读取。`project_id` 是服务器白名单键，调用者不能提交工作区路径。
如果需要查询 SQL Server，应在**运行 Worker 的同一服务账户**下配置现有数据库连接和凭据；
数据目录也必须与该配置一致。

在两个 PowerShell 窗口分别运行：

```powershell
D:\python\Scripts\autocoding-api.exe --config deploy\api\server.local.json --host 127.0.0.1 --port 8000
D:\python\Scripts\autocoding-api-worker.exe --config deploy\api\server.local.json
```

`GET http://127.0.0.1:8000/healthz` 只说明 HTTP 进程可用；`GET /readyz` 在 Worker
心跳有效时返回 200，否则返回 503。接口进程可以有多个请求线程，但每个数据目录只能运行
一个 Worker，防止并行修改同一个工作区。以上是手动启动方式；开机启动和故障重启由后续部署
阶段配置进程管理器。远程开放前需要 HTTPS 入口。

## PowerShell 调用示例

所有业务接口都需要 `Authorization: Bearer <token>`；每个新的 POST 操作都需要一个新的
`Idempotency-Key`。请求超时后重试**相同**操作时沿用原键，这样不会重复执行。先创建任务：

```powershell
$apiBase = 'http://127.0.0.1:8000'
$apiToken = '<粘贴 server.local.json 中配置的 Token>'
$headers = @{ Authorization = "Bearer $apiToken"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$body = @{ workflow = 'incident'; project_id = 'demo'; message = '订单页面查询失败，请先诊断原因'; page_hint = '订单页面' } | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/tasks" -Headers $headers -ContentType 'application/json' -Body $body
$job
```

接口立刻返回 HTTP 202，包含 `job_id` 和 `task_id`。轮询本次操作，结束后查询任务：

```powershell
do {
    Start-Sleep -Seconds 2
    $operation = Invoke-RestMethod -Uri "$apiBase/v1/jobs/$($job.job_id)" -Headers $headers
} while ($operation.status -in @('queued', 'running'))
$task = Invoke-RestMethod -Uri "$apiBase/v1/tasks/$($job.task_id)" -Headers $headers
$task | ConvertTo-Json -Depth 12
```

`job.status=succeeded` 表示这一条请求执行结束；任务的实际结果看 `task.state`、
`task.summary` 和 `task.result`。若 `task.state=waiting_input`，可追加消息：

```powershell
$replyHeaders = @{ Authorization = "Bearer $apiToken"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$reply = @{ message = '补充：错误只发生在已付款订单'; expected_version = $task.version } | ConvertTo-Json
$nextJob = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/tasks/$($job.task_id)/messages" -Headers $replyHeaders -ContentType 'application/json' -Body $reply
```

开发工作流只需把创建任务时的 `workflow` 改成 `development`。可用
`GET /v1/projects` 查看当前 Token 可访问的项目 ID。服务自动生成 OpenAPI 文档：
`http://127.0.0.1:8000/docs`，可在浏览器中先使用 `Authorize` 输入 Token 再试调接口。

## 状态和可靠性边界

| 操作状态 | 含义 | 客户端动作 |
| --- | --- | --- |
| `queued` | 已落盘，等待 Worker | 继续轮询 |
| `running` | Worker 已领取；`progress` 保存最近的安全阶段 | 继续轮询 |
| `succeeded` | 本次操作结束，业务任务可能在等待输入/审批 | 查询任务 |
| `failed` | 操作未成功接受，未自动重试 | 查看任务后换新幂等键重试适用操作 |
| `recovery_required` | 领取后中断或执行异常；禁止自动重放 | 查询任务，再调用 `/resume` 显式只读恢复 |

查询任务返回 `state`、`version`、`summary`、`result`、最近 100 条非系统消息及最新操作。
追加消息必须提交刚读到的 `expected_version`。版本变化返回 409，重新查询并判断是否仍应发送。
任务和操作只允许原 Token 对应的用户读取，且用户需仍有项目访问权。配置撤销后未执行的
操作也会在 Worker 阶段再次检查。普通消息永远不会替代修改审批。

如果领取后的操作中断，必须由用户确认后只读恢复；先重新获取任务版本：

```powershell
$task = Invoke-RestMethod -Uri "$apiBase/v1/tasks/$($job.task_id)" -Headers $headers
$resumeHeaders = @{ Authorization = "Bearer $apiToken"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$resume = @{ expected_version = $task.version } | ConvertTo-Json
$recoveryJob = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/tasks/$($job.task_id)/resume" -Headers $resumeHeaders -ContentType 'application/json' -Body $resume
```

排队中断之前尚未被领取的操作仍会在 Worker 重启后执行。修改过程如果中断，既有引擎的
恢复规则继续适用；服务不会自动重放写操作。本版还没有正式的系统服务安装脚本、钉钉对接、
事件流或远程审批接口；当前接口适合先完成可控的提交、对话和查询联调。
