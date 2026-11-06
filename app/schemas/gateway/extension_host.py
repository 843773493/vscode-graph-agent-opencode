"""宿主通道边界；公开结构以 Gateway proto 为源，保留内部业务校验。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId

BackendId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


class _WorkspaceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    protocol_version: int = Field(ge=1, le=1)
    gateway_id: CanonicalGatewayId
    workspace_ids: list[CanonicalWorkspaceId] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_workspaces(self) -> _WorkspaceView:
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("Node 宿主工作区视图不能重复")
        return self


class ExtensionHostTargetRequest(_WorkspaceView):
    backend_id: BackendId | None = None


class ExtensionHostOpenRequest(_WorkspaceView):
    backend_id: BackendId
    view_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class ExtensionHostConnectionDTO(ExtensionHostOpenRequest):
    client_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class ExtensionHostRuntimeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    vscode_version: str = Field(min_length=1)
    node_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ExtensionHostTargetDTO(_WorkspaceView):
    backend_id: BackendId
    source: Literal["live"] = "live"
    platform: str = Field(min_length=1)
    channel_state: Literal["available", "unavailable", "draining"] = Field(
        description="当前绑定与平台支持的通道能力；不代表扩展已激活或完整 API 已接入")
    reason: Literal["runtime_missing", "platform_unsupported", "backend_draining"] | None = None
    runtime: ExtensionHostRuntimeDTO | None = None

    @model_validator(mode="after")
    def consistent_availability(self) -> ExtensionHostTargetDTO:
        expected_state = {None: "available", "runtime_missing": "unavailable",
                          "platform_unsupported": "unavailable", "backend_draining": "draining"}[self.reason]
        if self.channel_state != expected_state or (self.channel_state == "available" and self.runtime is None):
            raise ValueError("Node 宿主通道状态与实际运行时能力不符")
        return self
