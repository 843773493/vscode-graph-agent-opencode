from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class McpInvocationReceipt(BaseModel):
    """本地等待结束不代表远端副作用已停止；不保存异常自由正文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["error"] = "error"
    code: Literal["mcp_outcome_unknown"] = "mcp_outcome_unknown"
    effect: Literal["unknown"] = "unknown"
    retryable: Literal[False] = False
    tool_name: str
    error_type: str
    cancellation_delivery: Literal["not_requested", "sent", "failed"] = "not_requested"
    recovery: Literal["verify_remote_state_before_new_action"] = "verify_remote_state_before_new_action"

    def as_artifact(self) -> dict[str, object]:
        return {"mcp_outcome": self.model_dump()}

    @classmethod
    def from_artifact(cls, artifact: object) -> McpInvocationReceipt | None:
        if not isinstance(artifact, dict) or "mcp_outcome" not in artifact:
            return None
        return cls.model_validate(artifact["mcp_outcome"])


class McpToolDTO(BaseModel):
    tool_id: str
    server_id: str
    remote_name: str
    description: str


class McpServerDTO(BaseModel):
    server_id: str
    transport: Literal["stdio", "streamable_http"]
    enabled: bool
    status: Literal["disabled", "ready", "failed", "authorization_required"]
    tools: list[McpToolDTO] = Field(default_factory=list)


class McpConnectionOperationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    connection_id: str
    server_id: str
    action: Literal["connect", "disconnect"]
    status: Literal["pending", "waiting_user", "verifying", "succeeded", "failed", "expired", "cancelled"]
    stage: Literal["queued", "authorization", "initialize", "catalog", "disconnect", "complete", "cleanup", "reconcile"]
    created_at: float
    deadline: float
    updated_at: float
    error_code: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "expired", "cancelled"}


class McpConnectionRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_:-]+$")
    server_id: str = Field(min_length=1, max_length=64)
    action: Literal["connect", "disconnect"]


class McpAuthorizationDTO(BaseModel):
    """仅供已认证用户读取；不得用于普通状态、工具结果或会话正文。"""

    authorization_url: str = Field(repr=False)


class McpAuthorizationCallbackDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    code: str = Field(min_length=1, max_length=8192, repr=False)
    state: str = Field(min_length=1, max_length=1024, repr=False)
