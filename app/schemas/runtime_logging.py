"""进程内部日志应用合同，不属于工作区业务协议。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.logging_contract import load_runtime_log_contract


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
