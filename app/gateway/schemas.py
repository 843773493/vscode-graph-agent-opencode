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


class ActivateGatewayWorkspaceResultDTO(BaseModel):
    active_workspace_id: str


class GatewayHealthDTO(BaseModel):
    status: Literal["ok"] = "ok"
    active_workspace_id: str | None = None
