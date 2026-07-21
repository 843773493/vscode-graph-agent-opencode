from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GatewayConnectionKind = Literal["local", "remote_gateway"]
GatewayWorkspaceStatus = Literal["ready", "offline"]
GatewayServiceStatus = Literal["ready", "offline", "unavailable"]
GatewayRuntimeAction = Literal[
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


class GatewayConfigReloadStatusDTO(BaseModel):
    available: bool = False
    healthy: bool | None = None
    revision: str | None = None
    restart_required: bool = False
    reason: Literal[
        "invalid_config",
        "restart_required",
        "apply_failed",
    ] | None = None
    changed_sections: list[str] = Field(default_factory=list)
    last_error: str | None = None
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


class GatewayWorkspaceDTO(BaseModel):
    workspace_id: str
    parent_workspace_id: str | None = None
    name: str
    root_path: str
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


class FederationProtocolManifestDTO(BaseModel):
    protocol_version: int
    gateway_id: str
    federation_depth: Literal[0] = 0
    capabilities: list[str] = Field(default_factory=list)


class FederationWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    managed: bool
    connection_kind: Literal["local"]
    services: list[Literal["workspace_api", "terminal_manager", "browser_manager"]] = (
        Field(default_factory=list)
    )


class FederationWorkspaceListDTO(BaseModel):
    protocol_version: int
    gateway_id: str
    items: list[FederationWorkspaceDTO] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


class GatewayManagedWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    status: GatewayWorkspaceStatus
    removable: bool
    system_default: bool


class GatewayManagedWorkspaceListDTO(BaseModel):
    gateway_connection_id: str | None = None
    gateway_id: str
    gateway_name: str
    connection_kind: Literal["local", "remote_gateway"]
    items: list[GatewayManagedWorkspaceDTO] = Field(default_factory=list)


class GatewayInboundPeerDTO(BaseModel):
    connection_id: str
    peer_gateway_id: str
    credential_expires_at: str


class GatewayInboundWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    status: GatewayWorkspaceStatus
    managed: bool
    system_default: bool


class GatewayInboundAccessListDTO(BaseModel):
    gateway_id: str
    peers: list[GatewayInboundPeerDTO] = Field(default_factory=list)
    items: list[GatewayInboundWorkspaceDTO] = Field(default_factory=list)


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


class GatewayRuntimeBlockerDTO(BaseModel):
    kind: Literal["job", "tool", "background_task"]
    resource_id: str
    session_id: str
    status: str
    detail: str | None = None


class GatewayRuntimeRestartResultDTO(BaseModel):
    workspace_id: str
    status: Literal["restarted", "blocked"]
    forced: bool
    blockers: list[GatewayRuntimeBlockerDTO] = Field(default_factory=list)
    workspaces: GatewayWorkspaceListDTO


class AddLocalWorkspaceRequest(BaseModel):
    root_path: str = Field(description="本机工作区绝对路径")
    name: str | None = Field(default=None, description="工作区显示名称")
    backend_url: str | None = Field(
        default=None,
        description="已有后端 URL；未提供时 Gateway 会为该工作区启动本机后端。",
    )


class AddRemoteGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, description="远程 Gateway 显示名称")
    connection_workspace_id: str | None = Field(
        default=None,
        description="复用已连接远程 Gateway 的 SSH 主机和凭据",
    )
    ssh_config_host: str | None = Field(
        default=None,
        description="复用用户 ~/.ssh/config 中的 Host 别名",
    )
    remote_gateway_port: int = Field(
        default=8014,
        ge=1,
        le=65535,
        description="远端 Gateway loopback 端口",
    )

    @model_validator(mode="after")
    def validate_connection_source(self) -> "AddRemoteGatewayRequest":
        connection_source_count = sum(
            bool(value) for value in (self.connection_workspace_id, self.ssh_config_host)
        )
        if connection_source_count != 1:
            raise ValueError(
                "必须且只能选择一个已注册 SSH 连接或 ~/.ssh/config Host"
            )
        return self


# TODO: 前端与扩展完成同一发布周期升级后，删除旧类型别名。
AddSshWorkspaceRequest = AddRemoteGatewayRequest


class ReorderGatewayWorkspacesRequest(BaseModel):
    workspace_ids: list[str] = Field(description="按目标展示顺序排列的全部 Gateway 工作区 ID")


class UpdateGatewayWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, description="工作区显示名称")
    parent_workspace_id: str | None = Field(
        default=None,
        description="父工作区 ID；显式传入 null 表示移出父工作区",
    )

    @model_validator(mode="after")
    def validate_update_fields(self) -> "UpdateGatewayWorkspaceRequest":
        if not self.model_fields_set:
            raise ValueError("工作区更新至少需要一个字段")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Gateway 工作区名称不能为 null")
        return self


class ActivateGatewayWorkspaceResultDTO(BaseModel):
    active_workspace_id: str


class GatewayHealthDTO(BaseModel):
    status: Literal["ok"] = "ok"
    active_workspace_id: str | None = None


class WebUIMainAreaRatiosDTO(BaseModel):
    agent_sessions: float = Field(default=1, gt=0)
    chat: float = Field(default=1, gt=0)
    workspace_preview: float = Field(default=1, gt=0)
    auxiliary: float = Field(default=1, gt=0)


class WebUILayoutSettingsDTO(BaseModel):
    workbench_view: Literal["sessions", "gateway"] | None = None
    agent_sessions_panel_open: bool | None = None
    auxiliary_visible: bool | None = None
    main_area_ratios: WebUIMainAreaRatiosDTO | None = None
    workspace_preview_visible: bool | None = None
    workspace_preview_maximized: bool | None = None
    workspace_preview_file_paths: list[str] | None = Field(default=None, max_length=20)
    workspace_preview_active_file_path: str | None = Field(
        default=None,
        max_length=4096,
    )
    customizations_collapsed: bool | None = None
    customizations_height: int | None = Field(default=None, ge=80, le=520)
    content_view: Literal[
        "default",
        "events",
        "requests",
        "changes",
        "resources",
        "agent",
    ] | None = None
    pending_message_default_action: Literal["steering", "queued"] | None = None


class WebUISettingsDTO(BaseModel):
    layout: WebUILayoutSettingsDTO = Field(default_factory=WebUILayoutSettingsDTO)
    recent_local_workspace_paths: list[str] = Field(default_factory=list)


class WebUISettingsUpdateDTO(BaseModel):
    layout: WebUILayoutSettingsDTO | None = None
    recent_local_workspace_paths: list[str] | None = None


class GatewayDirectoryEntryDTO(BaseModel):
    name: str
    path: str


class GatewayDirectoryListDTO(BaseModel):
    path: str
    parent_path: str | None = None
    home_path: str
    entries: list[GatewayDirectoryEntryDTO] = Field(default_factory=list)
    truncated: bool = False
    limit: int


class SshConnectionOptionDTO(BaseModel):
    connection_id: str
    source: Literal["boxteam", "ssh_config"]
    label: str
    host: str
    port: int
    username: str
    workspace_id: str | None = None
    ssh_config_host: str | None = None
    initial_path: str | None = None


class SshConnectionOptionListDTO(BaseModel):
    items: list[SshConnectionOptionDTO] = Field(default_factory=list)
