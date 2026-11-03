"""联邦搜索的控制面目标发现；公开字段由 Gateway proto 约束。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GatewaySearchWorkspaceOwnerDTO(BaseModel):
    gateway_id: str
    workspace_id: str = Field(description="目标后端的稳定 workspace UUID，不是任一 Gateway 的路由别名")


class GatewayWorkspaceDiscoveryHitDTO(BaseModel):
    owner: GatewaySearchWorkspaceOwnerDTO
    target_id: str
    name: str
    navigation_node_id: str
    breadcrumb_node_ids: list[str]
    breadcrumb_names: list[str]


class GatewayWorkspaceDiscoveryResultsDTO(BaseModel):
    gateway_id: str
    query: str
    resolved_at: datetime
    source: Literal["registry"] = "registry"
    items: list[GatewayWorkspaceDiscoveryHitDTO] = Field(default_factory=list)
    cursor: str | None = None
    complete: bool
