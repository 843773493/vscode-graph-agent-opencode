from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.gateway.theme.defaults import DEFAULT_THEME_BACKGROUND_OVERLAY

GatewayConnectionKind = Literal["local", "remote_gateway"]
GatewayWorkspaceStatus = Literal["ready", "offline"]
GatewayServiceStatus = Literal["ready", "offline", "unavailable"]
PortForwardProtocol = Literal["http", "https", "tcp"]
PortForwardStatus = Literal["starting", "active", "error", "stopped"]
GatewayRuntimeAction = Literal[
    "start_managed_backend",
    "safe_restart_managed_backend",
    "force_restart_managed_backend",
    "reconnect_remote_gateway",
    "probe_external_backend",
]
GatewayDiagnosticStatus = Literal["ready", "degraded", "offline"]
GatewayDiagnosticLogStatus = Literal["available", "empty", "unavailable"]
GatewayDiagnosticLogSource = Literal["gateway", "workspace"]


class GatewayServiceStatusDTO(BaseModel):
    status: GatewayServiceStatus
    health_path: str
    local_url: str | None = None
    local_port: int | None = None
    remote_host: str | None = None
    remote_port: int | None = None
    error: str | None = None


class CreatePortForwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    remote_port: int = Field(ge=1, le=65535)
    local_port: int | None = Field(default=None, ge=1, le=65535)
    protocol: PortForwardProtocol = "http"
    label: str | None = Field(default=None, max_length=120)


class ChangePortForwardLocalPortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_port: int = Field(ge=1, le=65535)


class ChangePortForwardLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, max_length=120)


class PortForwardDTO(BaseModel):
    forward_id: str
    workspace_id: str
    connection_id: str
    remote_host: Literal["127.0.0.1"]
    remote_port: int
    local_host: Literal["127.0.0.1"]
    local_port: int
    protocol: PortForwardProtocol
    label: str | None
    status: PortForwardStatus
    error: str | None
    local_url: str | None


class PortForwardListDTO(BaseModel):
    items: list[PortForwardDTO]


class GatewayConfigReloadStatusDTO(BaseModel):
    available: bool = False
    healthy: bool | None = None
    revision: str | None = None
    restart_required: bool = False
    reason: (
        Literal[
            "invalid_config",
            "restart_required",
            "apply_failed",
        ]
        | None
    ) = None
    changed_sections: list[str] = Field(default_factory=list)
    last_error: str | None = None
    error: str | None = None


class GatewayConfigSourceDTO(BaseModel):
    path: str
    layer: Literal["inline", "user", "user_local"]
    precedence: int
    loaded: bool


class GatewayConfigSourcesDTO(BaseModel):
    revision: str
    schema_path: str
    sources: list[GatewayConfigSourceDTO] = Field(default_factory=list)


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


class GatewayRuntimeStateResultDTO(BaseModel):
    workspace_id: str
    status: Literal["started", "stopped", "blocked"]
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
    host: str | None = Field(default=None, description="手动连接的 SSH 主机")
    port: int = Field(default=22, ge=1, le=65535, description="手动连接的 SSH 端口")
    username: str | None = Field(default=None, description="手动连接的 SSH 用户名")
    private_key_path: str | None = Field(
        default=None,
        description="手动连接使用的 SSH 私钥路径",
    )
    remote_gateway_port: int = Field(
        default=8014,
        ge=1,
        le=65535,
        description="远端 Gateway loopback 端口",
    )

    @model_validator(mode="after")
    def validate_connection_source(self) -> AddRemoteGatewayRequest:
        explicit_values = (self.host, self.username, self.private_key_path)
        has_explicit_source = any(bool(value) for value in explicit_values)
        connection_source_count = sum(
            bool(value)
            for value in (
                self.connection_workspace_id,
                self.ssh_config_host,
                has_explicit_source,
            )
        )
        if connection_source_count != 1:
            raise ValueError(
                "必须且只能选择一个已注册 SSH 连接、~/.ssh/config Host 或手动 SSH 连接"
            )
        if has_explicit_source and not all(bool(value) for value in explicit_values):
            raise ValueError("手动 SSH 连接必须提供主机、用户名和私钥路径")
        return self


# TODO: 前端与扩展完成同一发布周期升级后，删除旧类型别名。
AddSshWorkspaceRequest = AddRemoteGatewayRequest


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


class GatewayHealthDTO(BaseModel):
    status: Literal["ok"] = "ok"
    active_workspace_id: str | None = None
    process_id: int = Field(gt=0)
    development_restart_available: bool = False


class GatewayDiagnosticLogDTO(BaseModel):
    log_id: str
    source: GatewayDiagnosticLogSource
    workspace_id: str | None = None
    workspace_name: str | None = None
    service: str
    label: str
    status: GatewayDiagnosticLogStatus
    tail: str = ""
    truncated: bool = False
    line_count: int = Field(default=0, ge=0)
    size_bytes: int = Field(default=0, ge=0)
    updated_at: str | None = None
    error: str | None = None


class GatewayDiagnosticWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    connection_kind: Literal["local", "remote_gateway"]
    status: GatewayWorkspaceStatus
    managed: bool
    system_default: bool
    connection_error: str | None = None


class GatewayDiagnosticsDTO(BaseModel):
    gateway_id: str
    gateway_name: str
    gateway_connection_id: str | None = None
    connection_kind: Literal["local", "remote_gateway"]
    status: GatewayDiagnosticStatus
    checked_at: str
    selected_workspace_id: str | None = None
    selected_log_id: str | None = None
    workspaces: list[GatewayDiagnosticWorkspaceDTO] = Field(default_factory=list)
    logs: list[GatewayDiagnosticLogDTO] = Field(default_factory=list)


class DevelopmentRuntimeRestartDTO(BaseModel):
    status: Literal["scheduled"] = "scheduled"
    previous_process_id: int = Field(gt=0)
    helper_process_id: int = Field(gt=0)
    delay_ms: int = Field(ge=0)


class WebUIMainAreaRatiosDTO(BaseModel):
    agent_sessions: float = Field(default=1, gt=0)
    chat: float = Field(default=1, gt=0)
    workspace_preview: float = Field(default=1, gt=0)
    auxiliary: float = Field(default=1, gt=0)


class WebUIWorkspaceBottomPanelSettingsDTO(BaseModel):
    visible: bool | None = None
    height: int | None = Field(default=None, ge=190, le=520)
    tab: Literal["terminal", "output", "gateway", "ports", "automation"] | None = None
    terminal_id: str | None = None


class WebUILayoutSettingsDTO(BaseModel):
    workbench_view: Literal["sessions", "gateway"] | None = None
    agent_sessions_panel_open: bool | None = None
    chat_visible: bool | None = None
    auxiliary_visible: bool | None = None
    panel_visible: bool | None = None
    auxiliary_tab: Literal["changes", "files", "automation", "resources"] | None = None
    auxiliary_tab_order: list[Literal["changes", "files", "automation", "resources"]] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
    )
    main_area_ratios: WebUIMainAreaRatiosDTO | None = None
    bottom_panel_by_workspace: dict[str, WebUIWorkspaceBottomPanelSettingsDTO] | None = Field(
        default=None,
        max_length=200,
    )
    workspace_preview_visible: bool | None = None
    workspace_preview_maximized: bool | None = None
    workspace_preview_file_paths: list[str] | None = Field(default=None, max_length=20)
    workspace_preview_active_file_path: str | None = Field(
        default=None,
        max_length=4096,
    )
    customizations_collapsed: bool | None = None
    customizations_height: int | None = Field(default=None, ge=80, le=520)
    panel_height: int | None = Field(default=None, ge=190, le=520)
    content_view: (
        Literal[
            "default",
            "events",
            "requests",
            "changes",
            "resources",
            "agent",
        ]
        | None
    ) = None
    pending_message_default_action: Literal["steering", "queued"] | None = None


class WebUISessionSidebarSettingsDTO(BaseModel):
    filter_mode: Literal["all", "current", "attachments", "agent", "named"] = "all"
    sort_mode: Literal["created", "updated"] = "updated"
    grouping_mode: Literal["workspace", "time"] = "workspace"
    workspace_group_capped: bool = True
    collapsed_workspace_ids: list[str] = Field(default_factory=list, max_length=1000)
    collapsed_session_ids: list[str] = Field(default_factory=list, max_length=5000)
    expanded_root_tree_ids: list[str] = Field(default_factory=list, max_length=1000)
    collapsed_section_ids: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator(
        "collapsed_workspace_ids",
        "collapsed_session_ids",
        "expanded_root_tree_ids",
        "collapsed_section_ids",
    )
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        if any(len(value) > 4096 for value in values):
            raise ValueError("UI 设置 ID 长度不能超过 4096")
        return sorted(set(values))


class WebUIWorkspaceFileTreeSettingsDTO(BaseModel):
    expanded_paths_by_workspace: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=200,
    )

    @field_validator("expanded_paths_by_workspace")
    @classmethod
    def normalize_expanded_paths(
        cls,
        values: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for workspace_id, paths in values.items():
            if len(workspace_id) > 4096:
                raise ValueError("文件树工作区 ID 长度不能超过 4096")
            if len(paths) > 1000 or any(len(path) > 4096 for path in paths):
                raise ValueError("单个工作区最多保存 1000 个长度不超过 4096 的路径")
            normalized[workspace_id] = sorted(set(paths))
        return normalized


class WebUIGatewayConsoleSettingsDTO(BaseModel):
    view: Literal["routing", "managed"] = "routing"


class GatewayThemeBackgroundDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["remote", "gateway_asset"]
    url: str | None = None
    asset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    position: str = Field(default="center", min_length=1, max_length=120)
    size: str = Field(default="cover", min_length=1, max_length=120)
    repeat: Literal["no-repeat", "repeat", "repeat-x", "repeat-y", "space", "round"] = (
        "no-repeat"
    )
    appearance: Literal["immersive", "theme"] = "immersive"
    overlay: str = Field(
        default=DEFAULT_THEME_BACKGROUND_OVERLAY,
        min_length=1,
        max_length=1000,
    )

    @model_validator(mode="after")
    def validate_source(self) -> GatewayThemeBackgroundDTO:
        if self.type == "remote":
            if not self.url or self.asset_id is not None:
                raise ValueError("remote 背景必须只提供 url")
        elif not self.asset_id or self.url is not None:
            raise ValueError("gateway_asset 背景必须只提供 asset_id")
        return self


class ResolvedGatewayThemeDTO(BaseModel):
    id: str
    label: str
    color_scheme: Literal["light", "dark"]
    tokens: dict[str, str]
    background_image_url: str | None = None


class GatewayThemeOptionDTO(BaseModel):
    id: str
    label: str
    extends: Literal["warm", "green", "blue"]
    source: Literal["builtin", "gateway_config"]
    preview_tokens: dict[str, str]
    background_image_url: str | None = None


class GatewayThemeCatalogDTO(BaseModel):
    current_theme_id: str
    items: list[GatewayThemeOptionDTO]
    current_theme: ResolvedGatewayThemeDTO


class GatewayUIAssetDTO(BaseModel):
    asset_id: str
    original_filename: str
    content_type: str
    size: int
    sha256: str
    imported_at: str
    url: str
    referenced_theme_ids: list[str] = Field(default_factory=list)


class GatewayUIAssetListDTO(BaseModel):
    items: list[GatewayUIAssetDTO] = Field(default_factory=list)


class WebUIThemeSettingsDTO(BaseModel):
    theme_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    background: GatewayThemeBackgroundDTO | None = None
    resolved_theme: ResolvedGatewayThemeDTO | None = None


class WebUIThemeSettingsUpdateDTO(BaseModel):
    theme_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    background: GatewayThemeBackgroundDTO | None = None


class WebUISettingsDTO(BaseModel):
    layout: WebUILayoutSettingsDTO = Field(default_factory=WebUILayoutSettingsDTO)
    session_sidebar: WebUISessionSidebarSettingsDTO = Field(
        default_factory=WebUISessionSidebarSettingsDTO
    )
    workspace_file_tree: WebUIWorkspaceFileTreeSettingsDTO = Field(
        default_factory=WebUIWorkspaceFileTreeSettingsDTO
    )
    gateway_console: WebUIGatewayConsoleSettingsDTO = Field(
        default_factory=WebUIGatewayConsoleSettingsDTO
    )
    theme: WebUIThemeSettingsDTO = Field(default_factory=WebUIThemeSettingsDTO)
    recent_local_workspace_paths: list[str] = Field(default_factory=list)


class WebUISettingsUpdateDTO(BaseModel):
    layout: WebUILayoutSettingsDTO | None = None
    session_sidebar: WebUISessionSidebarSettingsDTO | None = None
    workspace_file_tree: WebUIWorkspaceFileTreeSettingsDTO | None = None
    gateway_console: WebUIGatewayConsoleSettingsDTO | None = None
    theme: WebUIThemeSettingsUpdateDTO | None = None
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
