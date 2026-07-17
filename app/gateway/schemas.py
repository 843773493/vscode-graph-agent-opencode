from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GatewayConnectionKind = Literal["local", "ssh"]
GatewayWorkspaceStatus = Literal["ready", "offline"]
GatewayServiceStatus = Literal["ready", "offline", "unavailable"]


class GatewayServiceStatusDTO(BaseModel):
    status: GatewayServiceStatus
    health_path: str
    local_url: str | None = None
    local_port: int | None = None
    remote_host: str | None = None
    remote_port: int | None = None
    error: str | None = None


class GatewayWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    backend_url: str
    connection_kind: GatewayConnectionKind
    status: GatewayWorkspaceStatus
    active: bool = False
    managed: bool = False
    removable: bool = True
    system_default: bool = False
    remote: dict[str, object] = Field(default_factory=dict)
    services: dict[str, GatewayServiceStatusDTO] = Field(default_factory=dict)
    connection_error: str | None = None
    checked_at: str


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


class AddSshWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, description="工作区显示名称")
    connection_workspace_id: str | None = Field(
        default=None,
        description="复用已连接 SSH 工作区的主机和凭据",
    )
    ssh_config_host: str | None = Field(
        default=None,
        description="复用用户 ~/.ssh/config 中的 Host 别名",
    )
    remote_workspace_path: str = Field(description="本次要添加的远程工作区路径")

    @model_validator(mode="after")
    def validate_connection_source(self) -> "AddSshWorkspaceRequest":
        connection_source_count = sum(
            bool(value) for value in (self.connection_workspace_id, self.ssh_config_host)
        )
        if connection_source_count != 1:
            raise ValueError(
                "必须且只能选择一个已注册 SSH 连接或 ~/.ssh/config Host"
            )
        return self


class ReorderGatewayWorkspacesRequest(BaseModel):
    workspace_ids: list[str] = Field(description="按目标展示顺序排列的全部 Gateway 工作区 ID")


class RenameGatewayWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(description="工作区显示名称")


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
