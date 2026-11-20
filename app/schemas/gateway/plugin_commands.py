"""具名插件命令的注册、调用、进度和取消合同。"""

from __future__ import annotations

from boxteam_plugin_sdk.commands import (
    PluginCommandContribution,
    PluginCommandRegistration,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.schemas.canonical_params import (
    CanonicalSessionId,
    CanonicalThreadId,
    CanonicalWorkspaceId,
)
from app.schemas.gateway.service_plugin import (
    PluginBinding,
    PluginKeybindingContribution,
    PluginMenuContribution,
    PluginTarget,
    PluginVersionRequest,
)


class PluginContributionTarget(PluginTarget):
    workspace_id: CanonicalWorkspaceId | None = None


class PluginCommandPublication(BaseModel):
    """原绑定的窗口贡献投影，不含进程地址、凭据或插件参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding: PluginBinding
    revision: str
    state: str
    error: str | None = None
    instance_id: str | None = None
    commands: list[PluginCommandContribution]
    registrations: list[PluginCommandRegistration]
    menus: list[PluginMenuContribution]
    keybindings: list[PluginKeybindingContribution]


class PluginCommandSnapshotDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_id: str
    workspace_id: CanonicalWorkspaceId | None = None
    publications: list[PluginCommandPublication]


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
