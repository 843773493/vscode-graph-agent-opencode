"""联邦搜索及可选范围预览；网络编码复用原请求级 SSE 生命周期。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_NO_CACHE_HEADERS,
    stream_sse_with_heartbeat,
)
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry, get_workspace_navigation_store
from app.gateway.search.scope import SearchScopeError, resolve_search_scope
from app.gateway.search.service import GatewaySearchService
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.gateway.search import (
    GatewaySearchScopeRequest,
    GatewaySearchScopeResolutionDTO,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.sse import sse_responses
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)

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


def _encode_search_sse(event: FederatedSearchEventDTO) -> bytes:
    return f"id: {event.sequence}\nevent: search\ndata: {event.model_dump_json()}\n\n".encode()


_SEARCH_RESPONSES = sse_responses("请求级联邦搜索流", {"search": FederatedSearchEventDTO},
                                 models={"FederatedSearchEventDTO": FederatedSearchEventDTO})


@router.post(
    "", response_class=StreamingResponse, dependencies=[Depends(verify_user_access_for_proxy)],
    responses=_SEARCH_RESPONSES,
)
@router.post(
    "/agent", response_class=StreamingResponse,
    description="本机 Agent／脚本只读搜索；本机私有凭据授权，不创建浏览器访问租约。与浏览器入口共用同一查询及调度。",
    responses=_SEARCH_RESPONSES,
)
async def search(
    payload: FederatedSearchRequest,
    request: Request,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    navigation: Annotated[WorkspaceNavigationStore, Depends(get_workspace_navigation_store)],
):
    try:
        execution = GatewaySearchService(
            registry, navigation, _http_client(request), gateway_id=request.app.state.gateway_id,
            catalog_mirror=getattr(request.app.state, "session_catalog_search_service", None),
        ).prepare(payload, request_id=request_id, encode=_encode_search_sse)
    except SearchScopeError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)}) from error
    except TimeoutError as error:
        raise HTTPException(409, detail={"code": "deadline", "message": str(error)}) from error
    async def frames():
        events = execution.events
        try:
            async for _event in events:
                yield execution.encoder.frame
        finally:
            await events.aclose()

    return StreamingResponse(stream_sse_with_heartbeat(
        frames(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS, close_source_on_exit=True,
    ), media_type="text/event-stream", headers={**SSE_NO_CACHE_HEADERS, "X-Request-ID": request_id})
