"""只从原 registry／navigation 冻结和重解范围，不探测健康或启动目标。"""

from datetime import UTC, datetime

from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.workspace_discovery import discover_workspaces
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.gateway.search import (
    MAX_SEARCH_WORKSPACES,
    GatewaySearchScopeBranchDTO,
    GatewaySearchScopeDTO,
    GatewaySearchScopeRequest,
    GatewaySearchScopeResolutionDTO,
    GatewaySearchWorkspaceOwnerDTO,
)


class SearchScopeError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def resolve_search_scope(
    registry: GatewayWorkspaceRegistry,
    navigation: WorkspaceNavigationStore,
    *,
    gateway_id: str,
    request: GatewaySearchScopeRequest,
) -> GatewaySearchScopeResolutionDTO:
    """首次范围全量校验；续查保留原集合和顺序，只重新解析各 owner 的当前路由。"""
    if request.mode == "resume":
        scope = request.scope
        assert scope is not None  # 请求模型已核验模式和参数。
        if scope.gateway_id != gateway_id:
            raise SearchScopeError("scope_gateway_mismatch", "冻结范围属于另一个 Gateway", status_code=409)
    else:
        owners: tuple[GatewaySearchWorkspaceOwnerDTO, ...]
        if request.mode == "all":
            # 沿原导航顺序冻结，不能只取在线或名称相关的工作区；一条额外结果可判定超限。
            page = discover_workspaces(
                registry, navigation, gateway_id=gateway_id, query="",
                limit=MAX_SEARCH_WORKSPACES, cursor=None,
            )
            if not page.complete:
                raise SearchScopeError("scope_limit", f"全部范围超过 {MAX_SEARCH_WORKSPACES} 个工作区，请明确缩小范围")
            owners = tuple(dict.fromkeys(item.owner for item in page.items))
        elif request.mode == "origin":
            assert request.origin is not None
            owners = (request.origin,)
        else:
            owners = tuple(dict.fromkeys(request.owners))
        if not owners:
            raise SearchScopeError("empty_scope", "没有可搜索的已登记工作区；空集合不能解释为全部范围")
        scope = GatewaySearchScopeDTO(
            gateway_id=gateway_id, resolved_at=datetime.now(UTC), mode=request.mode, owners=owners,
        )

    branches: list[GatewaySearchScopeBranchDTO] = []
    for owner in scope.owners:
        key = (owner.gateway_id, owner.workspace_id)
        if registry.has_target(key):
            branches.append(GatewaySearchScopeBranchDTO(owner=owner, state="registered"))
            continue
        message = f"工作区归属未登记或已移除: {owner.gateway_id}/{owner.workspace_id}"
        if request.mode != "resume":
            raise SearchScopeError("owner_unavailable", message, status_code=404)
        branches.append(GatewaySearchScopeBranchDTO(owner=owner, state="unavailable", code="owner_unavailable", message=message))
    return GatewaySearchScopeResolutionDTO(scope=scope, branches=branches)
