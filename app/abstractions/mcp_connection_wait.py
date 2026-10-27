"""MCP 长等待的跨层版本化恢复数据；不保存凭据或可执行业务参数。"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.internal_v2.mcp import McpConnectionOperationDTO


class McpWaitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(min_length=1)
    connection_id: str = Field(min_length=1)
    deadline: float = Field(gt=0, allow_inf_nan=False)
    continuation: str = Field(min_length=1, max_length=4096)
    model_call_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    origin_job_id: str = Field(min_length=1)
    origin_execution_id: str = Field(min_length=1)
    gateway_id: str | None
    request_id: str | None


class McpWaitCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: Literal["observe", "deliver", "settled"] = "observe"
    operation: McpConnectionOperationDTO | None = None
    wait_expired: bool = False
    delivery: str | None = None

    @model_validator(mode="after")
    def validate_phase(self) -> McpWaitCheckpoint:
        if self.phase == "deliver" and self.operation is None:
            raise ValueError("MCP 待投递 checkpoint 必须包含已观察操作")
        if self.phase == "observe" and (self.operation is not None or self.delivery is not None or self.wait_expired):
            raise ValueError("MCP 观察 checkpoint 不能包含完成事实")
        if self.phase == "settled" and self.delivery is None:
            raise ValueError("MCP 结算 checkpoint 必须包含投递结果")
        return self


def mcp_wait_task_id(session_id: str, thread_id: str, operation_id: str) -> str:
    digest = hashlib.sha256(f"mcp-wait\0{session_id}\0{thread_id}\0{operation_id}".encode()).hexdigest()
    return f"bgt_{digest}"
