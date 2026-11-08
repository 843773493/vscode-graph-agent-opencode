"""具名插件命令的注册、调用、进度和取消合同。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.schemas.canonical_params import (
    CanonicalSessionId,
    CanonicalThreadId,
    CanonicalWorkspaceId,
)
from app.schemas.gateway.service_plugin import PluginVersionRequest


class PluginCommandRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1)
    handler: str = Field(min_length=1)
    registration_id: str = Field(min_length=1)


class PluginCommandRegistrationsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(min_length=1)
    commands: list[PluginCommandRegistration]


class PluginCommandInstanceRequest(PluginVersionRequest):
    plugin_id: str = Field(min_length=1)
    workspace_id: CanonicalWorkspaceId | None = None
    instance_id: str = Field(min_length=1)


class PluginCommandCancelRequest(PluginCommandInstanceRequest):
    registration_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


class PluginCommandCallRequest(PluginCommandCancelRequest):
    arguments: dict[str, JsonValue]
    remaining_ms: int = Field(strict=True, gt=0, le=2_147_483_647)
    session_id: CanonicalSessionId | None = None
    thread_id: CanonicalThreadId | None = None

    @model_validator(mode="after")
    def context_belongs_to_workspace(self) -> PluginCommandCallRequest:
        if (self.session_id is None) != (self.thread_id is None):
            raise ValueError("命令会话上下文必须同时提供 session_id 和 thread_id")
        if self.session_id is not None and self.workspace_id is None:
            raise ValueError("命令会话上下文必须具有明确工作区")
        return self


class PluginCommandCancelDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    call_id: str
    accepted: bool


class PluginCommandEventDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(min_length=1)
    registration_id: str = Field(min_length=1)
    command_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    kind: Literal["started", "progress", "result", "error", "cancelled"]
    completed: float | None = Field(default=None, ge=0)
    total: float | None = Field(default=None, ge=0)
    message: str | None = None
    result: JsonValue = None
    error_code: str | None = None

    @model_validator(mode="after")
    def event_fields_match_kind(self) -> PluginCommandEventDTO:
        if self.kind != "result" and self.result is not None:
            raise ValueError("只有 result 事件可包含命令结果")
        if (self.error_code is not None) != (self.kind == "error"):
            raise ValueError("error 事件必须且只能携带 error_code")
        if self.kind != "progress" and (self.completed is not None or self.total is not None):
            raise ValueError("只有 progress 事件可包含进度数值")
        return self
