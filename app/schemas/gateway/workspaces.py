from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.gateway.config import GatewayConfigReloadStatusDTO

GatewayConnectionKind = Literal["local", "remote_gateway"]


GatewayWorkspaceStatus = Literal["ready", "offline"]


GatewayServiceStatus = Literal["ready", "offline", "unavailable"]


GatewayRuntimeAction = Literal[
    "start_managed_backend",
    "safe_restart_managed_backend",
    "force_restart_managed_backend",
    "reconnect_remote_gateway",
    "probe_external_backend",
]


class GatewayServiceStatusDTO(BaseModel):
    status: GatewayServiceStatus
    health_path: str
    local_url: str | None = None
    local_port: int | None = None
    remote_host: str | None = None
    remote_port: int | None = None
    error: str | None = None


class GatewayRemoteConnectionSummaryDTO(BaseModel):
    gateway_connection_id: str
    remote_workspace_id: str
    gateway_id: str
    name: str
    host: str
    port: int
    username: str
    ssh_config_host: str | None = None
    remote_gateway_port: int
    config_event_cursor: int | None = None
    config_reload_state: str | None = None
    restart_required: bool = False
    candidate_ref: str | None = None


class GatewayWorkspaceDTO(BaseModel):
    workspace_id: str
    backend_workspace_id: str | None = None
    backend_generation: str | None = None
    affected_workspace_ids: list[str] = Field(default_factory=list)
    parent_workspace_id: str | None = None
    name: str
    backend_url: str
    connection_kind: GatewayConnectionKind
    status: GatewayWorkspaceStatus
    active: bool = False
    managed: bool = False
    removable: bool = True
    system_default: bool = False
    runtime_action: GatewayRuntimeAction
    config_reload: GatewayConfigReloadStatusDTO = Field(
        default_factory=GatewayConfigReloadStatusDTO
    )
    remote: GatewayRemoteConnectionSummaryDTO | None = None
    services: dict[str, GatewayServiceStatusDTO] = Field(default_factory=dict)
    connection_error: str | None = None
    checked_at: str


class GatewayManagedWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    status: GatewayWorkspaceStatus
    removable: bool
    system_default: bool


class GatewayManagedWorkspaceListDTO(BaseModel):
    gateway_connection_id: str | None = None
    gateway_id: str
    gateway_name: str
    connection_kind: GatewayConnectionKind
    items: list[GatewayManagedWorkspaceDTO] = Field(default_factory=list)


class CreateFederationManagedWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    root_path: str = Field(min_length=1, description="远端 Gateway 主机上的绝对目录")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    create_directory: bool = Field(
        default=True,
        description="目录不存在时由远端 Gateway 创建目录",
    )


class CreateGatewayManagedWorkspaceRequest(CreateFederationManagedWorkspaceRequest):
    gateway_connection_id: str | None = Field(
        default=None,
        description="为空时管理本机 Gateway；否则管理指定远程 Gateway",
    )


class GatewayWorkspaceListDTO(BaseModel):
    active_workspace_id: str | None = None
    items: list[GatewayWorkspaceDTO] = Field(default_factory=list)


class AddLocalWorkspaceRequest(BaseModel):
    root_path: str = Field(description="本机工作区绝对路径")
    name: str | None = Field(default=None, description="工作区显示名称")
    backend_url: str | None = Field(
        default=None,
        description="已有后端 URL；未提供时 Gateway 会为该工作区启动本机后端。",
    )


class ReorderGatewayWorkspacesRequest(BaseModel):
    workspace_ids: list[str] = Field(
        description="按目标展示顺序排列的全部 Gateway 工作区 ID"
    )


class UpdateGatewayWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, description="工作区显示名称")
    parent_workspace_id: str | None = Field(
        default=None,
        description="父工作区 ID；显式传入 null 表示移出父工作区",
    )

    @model_validator(mode="after")
    def validate_update_fields(self) -> UpdateGatewayWorkspaceRequest:
        if not self.model_fields_set:
            raise ValueError("工作区更新至少需要一个字段")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Gateway 工作区名称不能为 null")
        return self


class ActivateGatewayWorkspaceResultDTO(BaseModel):
    active_workspace_id: str
