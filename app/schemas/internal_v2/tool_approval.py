"""工具审批的精确检查点、调用身份及控制回执。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolApprovalActionDTO(BaseModel):
    tool_call_id: str
    name: str
    args: dict[str, object]
    description: str | None = None
    allowed_decisions: list[Literal["approve", "edit", "reject", "respond"]]


class ToolApprovalDTO(BaseModel):
    approval_id: str
    checkpoint_id: str
    job_id: str
    session_id: str
    thread_id: str
    actions: list[ToolApprovalActionDTO]


class ToolApprovalEditedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    args: dict[str, object]


class ToolApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_call_id: str = Field(min_length=1)
    type: Literal["approve", "edit", "reject", "respond"]
    edited_action: ToolApprovalEditedAction | None = None
    message: str | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> ToolApprovalDecision:
        if (self.type == "edit") != (self.edited_action is not None):
            raise ValueError("只有 edit 决定必须包含 edited_action")
        if self.type == "respond" and self.message is None:
            raise ValueError("respond 决定必须包含 message")
        if self.type in {"approve", "edit"} and self.message is not None:
            raise ValueError("approve/edit 决定不接受 message")
        return self


class ToolApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(min_length=1)
    decisions: list[ToolApprovalDecision] = Field(min_length=1)


class ToolApprovalSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    responses: list[ToolApprovalResponse] = Field(min_length=1)


class ToolApprovalCommandDTO(BaseModel):
    command_id: str
    checkpoint_id: str
    status: Literal["accepted", "consumed", "rejected"]
