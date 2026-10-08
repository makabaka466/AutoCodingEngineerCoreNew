# HTTP API：独立接口与远程交互（0.12.0）

`src/autocoding_api/` 是独立交付层，HTTP 和执行进程分别运行；二者只调用现有
`AgentApplication` / `IncidentApplication`。任务首先持久化到
`<data_dir>/api/queue.sqlite3`，领域会话仍保存到既有 runtime SQLite。API 进程只读领域会话，不构建
Runtime 或执行恢复扫描。当前版本支持创建开发与异常诊断任务、追加普通消息、查询任务/操作、
显式只读恢复、按游标查询进度、手动进入异常处理，以及对已审阅方案批准或拒绝。

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
项目可配置 `git_remote`（远端名称如 `origin`，或仓库地址）和 `git_branch`（目标分支）。
两项必须同时设置；Worker 的服务账户需要已有的 Git 读取和推送凭据。使用远端地址时不要把
密码写进 URL。新任务开始前，Worker 只在本地分支匹配且工作区干净时快进同步；失败则不启动模型。
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

## 诊断后手动进入异常处理

只有诊断任务的 `can_remediate=true` 时才显示入口。诊断结果、原因和解决方向可从 `summary`
及 `result` 读取。用户决定进入后，把当前 `version`、`cycle_number` 原样提交：

```powershell
$task = Invoke-RestMethod -Uri "$apiBase/v1/tasks/$($job.task_id)" -Headers $headers
$handoffHeaders = @{ Authorization = "Bearer $apiToken"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$handoffBody = @{ expected_version = $task.version; cycle_number = $task.cycle_number } | ConvertTo-Json
$repairJob = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/tasks/$($job.task_id)/remediation" -Headers $handoffHeaders -ContentType 'application/json' -Body $handoffBody
```

返回的是新的**开发任务** `task_id`；原诊断任务仍独立保留。同一诊断轮次重复进入会复用该修复
任务。Worker 先只读核对证据并生成方案，不会因为调用此接口就修改文件。

## 审阅、批准或拒绝方案

修复操作结束后查询新任务。`state=waiting_modify_approval` 时，先把
`pending_approval.proposal` 完整展示给用户，包括逐项路径、涉及功能、当前行为、修改后行为、
目标效果、影响与风险、验证计划。只有用户明确确认后才能提交批准请求：

```powershell
$repair = Invoke-RestMethod -Uri "$apiBase/v1/tasks/$($repairJob.task_id)" -Headers $headers
$repair.pending_approval.proposal | ConvertTo-Json -Depth 12
$approveHeaders = @{ Authorization = "Bearer $apiToken"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$approvalBody = @{
    expected_version = $repair.version
    approval_id = $repair.pending_approval.approval_id
    scope = $repair.pending_approval.scope
} | ConvertTo-Json
$approvedJob = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/tasks/$($repairJob.task_id)/approve" -Headers $approveHeaders -ContentType 'application/json' -Body $approvalBody
```

`scope=modify` 只授权当前方案中的修改。完成后如果状态变为
`waiting_verify_approval`，要**重新查询任务、审阅验证动作，再用新的** `version`、
`approval_id`、`scope=verify` 调同一批准接口。不能沿用旧方案标识。

若用户拒绝方案，使用相同版本、方案标识与范围调用 `/reject`，可补充原因；任务会回到只读
调查和说明，不会执行被拒绝的写操作：

```powershell
$rejectBody = @{
    expected_version = $repair.version
    approval_id = $repair.pending_approval.approval_id
    scope = $repair.pending_approval.scope
    reason = '先不要修改代码，请补充风险说明'
} | ConvertTo-Json
$rejectedJob = Invoke-RestMethod -Method Post -Uri "$apiBase/v1/tasks/$($repairJob.task_id)/reject" -Headers $approveHeaders -ContentType 'application/json' -Body $rejectBody
```

普通 `/messages` 请求即使内容写了“同意”，也只作为用户消息进入只读分析，不能批准修改。
批准/拒绝在入队和执行前都检查版本、范围和方案标识；不符返回 409。重复提交相同请求时沿用
原 `Idempotency-Key`，查看原操作结果，不会执行第二次。

## Git 更新与发布

已有任务可调用 `POST /v1/tasks/{task_id}/git-sync`，正文为当前任务的
`{"expected_version": 3}`，并使用新的 `Idempotency-Key`。Worker 领取后检查版本，在
干净工作区中 fetch 并只做快进更新；待审批方案应先拒绝并重新调查，避免旧方案基于过期代码。

开发任务完成后，先调用 `GET /v1/tasks/{task_id}/git-preview`。响应包含目标远端、分支、
待提交文件清单和 `fingerprint`；将清单展示给用户审核。用户填写单行修改摘要后，调用
`POST /v1/tasks/{task_id}/git-publish`，正文示例：

```json
{"expected_version": 3, "fingerprint": "<预览返回的64位哈希>", "summary": "修复订单查询错误"}
```

接口会再次检查文件内容和远端版本，随后提交并正常推送，不使用强制推送。远端变更、文件变更、
本地冲突和凭据错误会停止操作。若进程在提交或推送之间中断，队列不会自动重放；请核对本地
HEAD 与远端分支后再人工处理。若本地恰好只有一个待推送提交且工作区干净，重新调用
`git-preview` 会返回 `pending_push=true`、原摘要与文件清单；用户再次确认后可用相同摘要
和新指纹调用 `git-publish`，仅重试推送。Git 同步和推送不执行模型，也不会代替业务验收。
成功后从操作的 `progress.commit` 读取本地已同步或已推送的提交 ID。

## 轮询进度

`GET /v1/tasks/{task_id}/events?after=0&limit=100` 返回该任务自己的持久化事件及
`next_cursor`。后续请求把游标放入 `after`；客户端重连后沿用最后一个游标即可继续读取。
事件种类包括 `job_queued`、`job_started`、`progress`、`job_finished`；`progress.data`
包含安全阶段标签、活动标志等，不提供模型推理过程、原始工具输出或原始业务数据。
客户端在操作为 `queued` 或 `running` 时显示加载动画；等待输入或审批时依任务状态显示具体
操作按钮。当前使用轮询，钉钉对接可由服务端适配层按此接口读取。

## 状态和可靠性边界

| 操作状态 | 含义 | 客户端动作 |
| --- | --- | --- |
| `queued` | 已落盘，等待 Worker | 继续轮询 |
| `running` | Worker 已领取；`progress` 保存最近的安全阶段 | 继续轮询 |
| `succeeded` | 本次操作结束，业务任务可能在等待输入/审批 | 查询任务 |
| `failed` | 操作未成功接受，未自动重试 | 查看任务后换新幂等键重试适用操作 |
| `recovery_required` | 领取后中断或执行异常；禁止自动重放 | 查询任务，再调用 `/resume` 显式只读恢复 |

查询任务返回 `state`、`version`、`cycle_number`、`summary`、`result`、
`pending_approval`、`can_remediate`、最近 100 条非系统消息及最新操作。
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
恢复规则继续适用；服务不会自动重放写操作。若审批的领域命令回执已落盘而队列尚未确认，
Worker 重启会识别回执并补齐队列结果，不会重新执行该审批。本版没有正式的系统服务安装脚本、
开机自启、外部 HTTPS 入口或钉钉适配层；这些属于下一阶段的常驻部署。
