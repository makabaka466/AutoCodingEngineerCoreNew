"""所有 HTTP 路由集中在此；认证用户由 Token 决定，不相信请求中的 actor。"""

import hashlib
import secrets
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from autocoding_api.config import ServerConfig
from autocoding_api.service import ApiService
from autocoding_api.store import ApiError

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)]


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: Literal["development", "incident"] = Field(description="工作流类型。")
    project_id: str = Field(min_length=1, max_length=100, description="服务器项目 ID。")
    message: Text = Field(description="用户问题或开发要求。")
    page_hint: Text | None = Field(default=None, description="异常诊断的页面线索。")
    external_reference: str | None = Field(
        default=None, max_length=256, description="可选外部业务引用，不影响权限。"
    )

    @model_validator(mode="after")
    def incident_fields(self):
        if self.workflow == "development" and (self.page_hint or self.external_reference):
            raise ValueError("page_hint 和 external_reference 仅适用于 incident")
        return self


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Text = Field(description="追加信息；普通消息不授予修改权限。")
    expected_version: int = Field(ge=0, description="查询任务时获取的版本。")


class ResumeTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int | None = Field(description="当前版本；尚未建立领域会话时为 null。", ge=0)


class RemediateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, description="已审阅的诊断任务版本。")
    cycle_number: int = Field(ge=1, description="已审阅的诊断轮次。")


class ApprovalAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, description="已审阅方案对应的任务版本。")
    approval_id: str = Field(
        min_length=64, max_length=64, pattern="^[0-9a-f]{64}$",
        description="从任务查询结果中读取的当前方案标识。",
    )
    scope: Literal["modify", "verify"] = Field(description="批准或拒绝的权限范围。")


class RejectAction(ApprovalAction):
    reason: str = Field(default="", max_length=2000, description="拒绝原因。")


def create_app(config: ServerConfig) -> FastAPI:
    service = ApiService(config)
    app = FastAPI(title="AutoCoding Agent API", version="0.11.0")
    app.state.service = service
    bearer = HTTPBearer(auto_error=False)

    def authenticate(
        credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        supplied = hashlib.sha256(
            (credential.credentials if credential else "").encode()
        ).digest()
        for owner, user in config.users.items():
            expected = hashlib.sha256(user.token.get_secret_value().encode()).digest()
            if credential and secrets.compare_digest(supplied, expected):
                return owner
        raise HTTPException(401, "认证失败", headers={"WWW-Authenticate": "Bearer"})

    User = Annotated[str, Depends(authenticate)]

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return JSONResponse(status_code=error.status, content={"detail": error.detail})

    @app.get("/healthz")
    def health():
        return {"status": "ok", "scope": "api", "worker_managed_separately": True}

    @app.get("/readyz")
    def ready():
        if not service.queue.worker_ready():
            return JSONResponse(status_code=503, content={"status": "worker_unavailable"})
        return {"status": "ready"}

    @app.get("/v1/projects")
    def projects(owner: User):
        return {"projects": [{"project_id": name} for name in config.users[owner].projects]}

    @app.post("/v1/tasks", status_code=202)
    def create(body: CreateTask, owner: User, key: Key):
        return service.create(owner, key, body.model_dump(mode="json"))

    @app.get("/v1/tasks/{task_id}")
    def task(task_id: UUID, owner: User):
        return service.get_task(owner, str(task_id))

    @app.get("/v1/tasks/{task_id}/events")
    def events(
        task_id: UUID, owner: User,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ):
        return service.events(owner, str(task_id), after, limit)

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: UUID, owner: User):
        return service.get_job(owner, str(job_id))

    @app.post("/v1/tasks/{task_id}/messages", status_code=202)
    def message(task_id: UUID, body: SendMessage, owner: User, key: Key):
        return service.submit(owner, str(task_id), key, "message", body.model_dump(mode="json"))

    @app.post("/v1/tasks/{task_id}/resume", status_code=202)
    def resume(task_id: UUID, body: ResumeTask, owner: User, key: Key):
        return service.submit(owner, str(task_id), key, "resume", body.model_dump(mode="json"))

    @app.post("/v1/tasks/{task_id}/remediation", status_code=202)
    def remediation(task_id: UUID, body: RemediateTask, owner: User, key: Key):
        return service.remediate(owner, str(task_id), key, body.model_dump(mode="json"))

    @app.post("/v1/tasks/{task_id}/approve", status_code=202)
    def approve(task_id: UUID, body: ApprovalAction, owner: User, key: Key):
        return service.submit(owner, str(task_id), key, "approve", body.model_dump(mode="json"))

    @app.post("/v1/tasks/{task_id}/reject", status_code=202)
    def reject(task_id: UUID, body: RejectAction, owner: User, key: Key):
        return service.submit(owner, str(task_id), key, "reject", body.model_dump(mode="json"))

    return app
