"""联邦搜索的范围预览 HTTP 边界，不启动领域查询。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry, get_workspace_navigation_store
from app.gateway.search.scope import SearchScopeError, resolve_search_scope
from app.schemas.gateway.search import (
    GatewaySearchScopeRequest,
    GatewaySearchScopeResolutionDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(prefix="/api/gateway/search", tags=["gateway-search"])


@router.post("/resolve", response_model=APIResponse[GatewaySearchScopeResolutionDTO])
async def resolve_scope(
    payload: GatewaySearchScopeRequest,
    request: Request,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    navigation: Annotated[WorkspaceNavigationStore, Depends(get_workspace_navigation_store)],
):
    try:
        result = resolve_search_scope(
            registry, navigation,
            gateway_id=request.app.state.gateway_id, request=payload,
        )
    except SearchScopeError as error:
        raise HTTPException(error.status_code, detail={
            "owner": "search_scope", "code": error.code, "message": str(error),
        }) from error
    return APIResponse(data=result, request_id=request_id)
