"""宿主审批与执行核对快照；不进入模型返回协议。"""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class FileFingerprint(BaseModel):
    """一份有界文件观测；missing 明确表示审批时计划新增文件。"""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="工作区内规范化的相对文件路径。")
    state: Literal["present", "missing", "unverified"] = Field(
        description="可读取文件、不存在文件或无法核实的文件。")
    sha256: str | None = Field(default=None, description="普通文件当前内容的宿主指纹。")


class ApprovalSnapshot(BaseModel):
    """不可变审批副本，防止后续方案对象原地修改后沿用授权。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid4()), description="独立审批快照编号。")
    approval_json: str = Field(description="规范 JSON 编码的完整用户待审方案与动作。")
    approval_sha256: str = Field(description="完整审批内容指纹。")
    context_sha256: str = Field(description="任务、周期、用户输入及当前调查上下文指纹。")
    cycle_number: int = Field(description="生成方案所属的任务周期。")
    planned_paths: tuple[str, ...] = Field(description="方案明确列出的可写文件范围。")
    files: tuple[FileFingerprint, ...] = Field(description="计划文件与当前读取依据的指纹。")
    issues: tuple[str, ...] = Field(default=(), description="需重新分析后才能批准的问题。")


class ImplementationReport(BaseModel):
    """文件级核对结果，不将模型自报改动当作完成或功能正确的证据。"""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = Field(description="本次执行获批的方案快照编号。")
    command_id: str = Field(description="本次实施命令编号，用于隔离其他轮次工具事件。")
    changed_paths: tuple[str, ...] = Field(default=(), description="计划内已观测到内容变化的文件。")
    unchanged_paths: tuple[str, ...] = Field(
        default=(), description="计划内当前未发生内容变化的文件。")
    out_of_scope_paths: tuple[str, ...] = Field(
        default=(), description="范围外写尝试或依据文件变化。")
    unverified_paths: tuple[str, ...] = Field(
        default=(), description="无法核对或写结果不确定的文件。")
    reported_only_paths: tuple[str, ...] = Field(
        default=(), description="仅模型报告改动但未获宿主证明的文件。")
    interrupted: bool = Field(default=False, description="实施是否被中断，不能视为整体成功。")
    limitations: tuple[str, ...] = Field(default=(), description="观测范围、归属及功能验证限制。")
