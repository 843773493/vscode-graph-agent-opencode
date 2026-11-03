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
from app.gateway.search.scheduler import (
    SearchJob,
    output_limited,
    plan_search,
    search_events,
)
from app.gateway.search.scope import SearchScopeError, resolve_search_scope
from app.gateway.search.transport import SearchTransport
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
    MAX_SEARCH_OUTPUT_BYTES,
    FederatedSearchEventDTO,
    FederatedSearchRequest,
    FederatedSearchSummaryDTO,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget

router = APIRouter(prefix="/api/gateway/search", tags=["gateway-search"])


class _SearchResponseEncoder:
    """在唯一编码边界计量，预留逐分支终态及汇总；只暂存下一帧。"""

    def __init__(self, jobs: list[SearchJob], scope: GatewaySearchScopeResolutionDTO, request_id: str) -> None:
        self.limit = MAX_SEARCH_OUTPUT_BYTES
        self.used = 0
        self._branches = {job.key: output_limited(job) for job in jobs}
        self._sizes = {key: len(value.model_dump_json().encode()) for key, value in self._branches.items()}
        self._summary_size = self._pending_size = sum(self._sizes.values())
        self._pending_count = len(jobs)
        # 最长序号及计数只用于小型信封的预留；正文仅在 publish 中编码一次。
        sequence = 2 * len(jobs) + 2
        branch = next(iter(self._branches.values()))
        self._terminal_overhead = len(self._encode(FederatedSearchEventDTO(
            sequence=sequence, request_id=request_id, type="branch", branch=branch,
        ))) - self._sizes[branch.branch]
        self._end_overhead = len(self._encode(FederatedSearchEventDTO(
            sequence=sequence, request_id=request_id, type="end",
            summary=FederatedSearchSummaryDTO(state="incomplete", branches=[], returned_results=sum(job.request.limit for job in jobs)),
        ))) + max(0, len(jobs) - 1)
        self.frame = self._encode(FederatedSearchEventDTO(sequence=1, request_id=request_id, type="scope", scope=scope))
        if len(self.frame) + self._reserve(self._summary_size, self._pending_size, self._pending_count) > self.limit:
            raise SearchScopeError("output_bytes", "范围及原续点超过搜索流字节上限，请缩小范围", status_code=413)

    @staticmethod
    def _encode(event: FederatedSearchEventDTO) -> bytes:
        return f"id: {event.sequence}\nevent: search\ndata: {event.model_dump_json()}\n\n".encode()

    def _reserve(self, summary_size: int, pending_size: int, pending_count: int) -> int:
        return summary_size + self._end_overhead + pending_size + pending_count * self._terminal_overhead

    def publish(self, event: FederatedSearchEventDTO) -> bool:
        if event.type == "scope":
            self.used += len(self.frame)
            return True
        frame = self._encode(event)
        if event.type == "end":
            if self.used + len(frame) > self.limit:
                raise RuntimeError("搜索流终态超过已预留的输出预算")
            self.frame = frame
            self.used += len(frame)
            return True
        branch = event.branch
        assert branch is not None
        key = branch.branch
        size = self._sizes[key] if branch is self._branches[key] else len(branch.model_dump_json().encode())
        summary_size = self._summary_size + size - self._sizes[key]
        pending_size = self._pending_size + size - self._sizes[key]
        pending_count = self._pending_count
        if event.type == "branch":
            pending_size -= size
            pending_count -= 1
        if self.used + len(frame) + self._reserve(summary_size, pending_size, pending_count) > self.limit:
            return False
        self._branches[key], self._sizes[key] = branch, size
        self._summary_size, self._pending_size, self._pending_count = summary_size, pending_size, pending_count
        self.frame = frame
        self.used += len(frame)
        return True


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
        budget = NameSearchBudget.create(payload.deadline_at)
        scope = resolve_search_scope(registry, navigation, gateway_id=request.app.state.gateway_id, request=payload.scope)
        jobs = plan_search(payload, scope)
        encoder = _SearchResponseEncoder(jobs, scope, request_id)
    except SearchScopeError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)}) from error
    except TimeoutError as error:
        raise HTTPException(409, detail={"code": "deadline", "message": str(error)}) from error
    transport = SearchTransport(
        registry, _http_client(request), gateway_id=request.app.state.gateway_id, request_id=request_id,
        catalog_mirror=getattr(request.app.state, "session_catalog_search_service", None),
    )

    async def frames():
        events = search_events(jobs, scope, request_id=request_id, query=transport.query, budget=budget, publish=encoder.publish)
        try:
            async for _event in events:
                yield encoder.frame
        finally:
            await events.aclose()

    return StreamingResponse(stream_sse_with_heartbeat(
        frames(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS, close_source_on_exit=True,
    ), media_type="text/event-stream", headers={**SSE_NO_CACHE_HEADERS, "X-Request-ID": request_id})
