from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.gateway.workspaces import GatewayConnectionKind
from app.schemas.internal_v2.workspace_resource import WorkspaceResourceDTO

WorkspaceNavigationNodeKind = Literal["workspace_folder", "workspace_ref"]
WorkspaceNavigationPlacementMode = Literal["before", "after", "last"]


class WorkspaceNavigationNodeDTO(BaseModel):
    node_id: str
    kind: WorkspaceNavigationNodeKind
    name: str
    parent_node_id: str | None = None
    workspace_id: str | None = None
    position: int = 0

    @model_validator(mode="after")
    def validate_target(self) -> WorkspaceNavigationNodeDTO:
        if self.kind == "workspace_ref" and self.workspace_id is None:
            raise ValueError("workspace_ref 缺少 workspace_id")
        if self.kind == "workspace_folder" and self.workspace_id is not None:
            raise ValueError("workspace_folder 不能包含 workspace_id")
        return self


class WorkspaceNavigationTreeDTO(BaseModel):
    revision: str
    nodes: list[WorkspaceNavigationNodeDTO] = Field(default_factory=list)


class WorkspaceNavigationBreadcrumbDTO(BaseModel):
    revision: str
    items: list[WorkspaceNavigationNodeDTO] = Field(default_factory=list)


class WorkspaceFolderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    parent_node_id: str | None = None
    position: int | None = None


class WorkspaceNavigationNodeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_node_id: str | None = None
    position: int | None = None

    @model_validator(mode="after")
    def require_field(self) -> WorkspaceNavigationNodeUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("导航节点更新至少需要一个字段")
        return self


class WorkspaceNavigationReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_node_id: str | None = None
    node_ids: list[str] = Field(min_length=1)


class WorkspaceNavigationPlacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    parent_node_id: str | None = None
    mode: WorkspaceNavigationPlacementMode
    target_node_id: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> WorkspaceNavigationPlacementRequest:
        if self.mode in {"before", "after"} and self.target_node_id is None:
            raise ValueError(f"{self.mode} placement 缺少 target_node_id")
        if self.mode == "last" and self.target_node_id is not None:
            raise ValueError("last placement 不应包含 target_node_id")
        if self.node_id == self.target_node_id:
            raise ValueError("导航节点不能相对自身排序")
        return self


class GatewayResourceScopeErrorDTO(BaseModel):
    scope_key: str
    label: str
    message: str


class GatewayResourceDTO(BaseModel):
    gateway_connection_id: str | None = None
    gateway_name: str
    # Gateway 注册表中的路由工作区 ID；内层 resource.workspace_id 是后端 owner UUID。
    workspace_id: str
    workspace_name: str
    connection_kind: GatewayConnectionKind
    session_id: str
    session_title: str
    resource: WorkspaceResourceDTO


class GatewayResourceListDTO(BaseModel):
    items: list[GatewayResourceDTO] = Field(default_factory=list)
    errors: list[GatewayResourceScopeErrorDTO] = Field(default_factory=list)


class GatewaySessionSearchMatchDTO(BaseModel):
    workspace_id: str
    workspace_name: str
    node_id: str
    node_kind: Literal["workspace_folder", "workspace", "folder", "session"]
    name: str
    session_id: str | None = None
    relative_path: str
    breadcrumb_names: list[str] = Field(default_factory=list)
    breadcrumb_node_ids: list[str] = Field(default_factory=list)


class GatewaySessionSearchWorkspaceStatusDTO(BaseModel):
    workspace_id: str
    workspace_name: str
    status: Literal["available", "stale", "unavailable"]
    error: str | None = None


class GatewaySessionSearchResultsDTO(BaseModel):
    items: list[GatewaySessionSearchMatchDTO] = Field(default_factory=list)
    workspaces: list[GatewaySessionSearchWorkspaceStatusDTO] = Field(default_factory=list)
    total: int = 0
