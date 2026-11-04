"""联邦搜索的控制面目标发现；公开字段由 Gateway proto 约束。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

MAX_SEARCH_WORKSPACES = 64


class GatewaySearchWorkspaceOwnerDTO(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1, description="目标后端的稳定 workspace UUID，不是任一 Gateway 的路由别名")


class GatewayWorkspaceDiscoveryHitDTO(BaseModel):
    owner: GatewaySearchWorkspaceOwnerDTO
    name: str
    navigation_node_id: str
    breadcrumb_node_ids: list[str]
    breadcrumb_names: list[str]


class GatewayNavigationFolderDiscoveryHitDTO(BaseModel):
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
    folders: list[GatewayNavigationFolderDiscoveryHitDTO] = Field(default_factory=list)
    cursor: str | None = None
    complete: bool


class GatewaySearchScopeDTO(BaseModel):
    """可跨请求传回的范围事实，不含临时路由、凭据或领域 cursor。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    gateway_id: str = Field(min_length=1)
    resolved_at: AwareDatetime
    mode: Literal["origin", "selected", "all"]
    owners: tuple[GatewaySearchWorkspaceOwnerDTO, ...] = Field(min_length=1, max_length=MAX_SEARCH_WORKSPACES)

    @model_validator(mode="after")
    def validate_owners(self) -> Self:
        if len(set(self.owners)) != len(self.owners):
            raise ValueError("冻结范围不得重复包含同一 owner")
        if self.mode == "origin" and len(self.owners) != 1:
            raise ValueError("发起工作区范围必须只有一个 owner")
        return self


class GatewaySearchScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["origin", "selected", "all", "resume"]
    origin: GatewaySearchWorkspaceOwnerDTO | None = None
    owners: tuple[GatewaySearchWorkspaceOwnerDTO, ...] = Field(default=(), max_length=MAX_SEARCH_WORKSPACES)
    scope: GatewaySearchScopeDTO | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        supplied = self.origin is not None, bool(self.owners), self.scope is not None
        expected = {
            "origin": (True, False, False), "selected": (False, True, False),
            "all": (False, False, False), "resume": (False, False, True),
        }
        if supplied != expected[self.mode]:
            raise ValueError("范围模式与参数不符；origin 需要发起 owner，selected 需要非空集合，resume 需要原冻结范围")
        return self


class GatewaySearchScopeBranchDTO(BaseModel):
    owner: GatewaySearchWorkspaceOwnerDTO
    state: Literal["registered", "unavailable", "ambiguous"]
    code: Literal["owner_unavailable", "owner_ambiguous"] | None = None
    message: str | None = None


class GatewaySearchScopeResolutionDTO(BaseModel):
    scope: GatewaySearchScopeDTO
    branches: list[GatewaySearchScopeBranchDTO]
