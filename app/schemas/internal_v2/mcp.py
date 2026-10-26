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
    status: Literal["disabled", "ready"]
    tools: list[McpToolDTO] = Field(default_factory=list)
