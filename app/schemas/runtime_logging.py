"""进程内部日志状态与应用合同，不属于工作区业务协议。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.logging_contract import load_runtime_log_contract

RuntimeLoggingService = Literal["launcher", "gateway", "backend", "terminal", "browser"]
RuntimeLoggingHttpService = Literal["gateway", "backend", "terminal", "browser"]


class RuntimeLoggingSink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    source: str = Field(min_length=1)
    device: str = Field(min_length=1)
    inode: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        # 此内部通道只连接本机产品进程，路径由当前目标机器验证。
        if not Path(value).is_absolute():
            raise ValueError("日志去向必须是本机绝对路径")
        return value


class RuntimeLoggingStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    service: RuntimeLoggingService
    pid: int = Field(gt=0)
    runtime_instance_id: str = Field(min_length=1)
    applied_revision: str | None
    level: Literal["debug", "info", "warning", "error", "critical"]
    pretty: bool
    log_sink: RuntimeLoggingSink | None


class RuntimeLoggingApply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    runtime_instance_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    expected_revision: str | None
    level: str
    pretty: bool

    @field_validator("revision")
    @classmethod
    def require_revision(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("日志 revision 不能为空白")
        return value

    @field_validator("level")
    @classmethod
    def normalize_level(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in load_runtime_log_contract().levels:
            raise ValueError("不支持的日志级别")
        return normalized
