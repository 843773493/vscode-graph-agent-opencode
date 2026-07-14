from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


GatewayConnectionKind = Literal["local", "ssh"]
GatewayWorkspaceStatus = Literal["ready", "offline"]


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
    name: str | None = Field(default=None, description="工作区显示名称")
    host: str = Field(description="SSH 主机")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(description="SSH 用户名")
    private_key_path: str = Field(description="Gateway 所在机器可读取的私钥路径")
    remote_backend_host: str = Field(default="127.0.0.1")
    remote_backend_port: int = Field(ge=1, le=65535)
    remote_workspace_path: str = Field(description="本次要添加的远程工作区路径")


class ReorderGatewayWorkspacesRequest(BaseModel):
    workspace_ids: list[str] = Field(description="按目标展示顺序排列的全部 Gateway 工作区 ID")


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


class LocalDirectoryEntryDTO(BaseModel):
    name: str
    path: str


class LocalDirectoryListDTO(BaseModel):
    path: str
    parent_path: str | None = None
    home_path: str
    entries: list[LocalDirectoryEntryDTO] = Field(default_factory=list)
    truncated: bool = False
    limit: int
