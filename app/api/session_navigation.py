from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.abstractions.state_conflict import ClientStateConflictError
from app.api.deps import (
    get_request_id,
    get_session_catalog_service,
    get_session_generation_service,
    verify_local_token,
)
from app.api.errors import (
    client_error_message,
    not_found_http_error,
    state_conflict_error,
    state_conflict_http_error,
)
from app.core.trace_middleware import (
    require_current_gateway_id,
    require_current_navigation_actor,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionCatalogExportDTO,
    SessionCatalogPageDTO,
    SessionCatalogSearchResultsDTO,
    SessionGenerationCapabilitiesDTO,
    SessionGenerationExecuteRequest,
    SessionGenerationExecuteResultDTO,
)
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationPreviewDTO,
    SessionGenerationPreviewRequest,
)
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationEventsPageDTO,
    NavigationMutationEnqueueRequest,
    NavigationMutationEnqueueResultDTO,
    NavigationMutationStatusPageDTO,
    NavigationSnapshotDTO,
)
from app.services.business.session_generation import SessionGenerationService
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_navigation.operations_service import (
    NavigationAuthScope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationBackpressureError,
    NavigationMutationConflictError,
    NavigationQueueOwnerError,
)

router = APIRouter(tags=["session-navigation"])


def _navigation_scope(service: SessionCatalogService) -> NavigationAuthScope:
    """构造导航 operation scope：Gateway/actor 来自已认证代理上下文。"""
    return NavigationAuthScope(
        gateway_id=require_current_gateway_id("Session catalog navigation operation"),
        workspace_id=service.workspace_id,
        actor=require_current_navigation_actor("Session catalog navigation operation"),
    )


@router.post(
    "/session-catalog/operations:enqueue",
    status_code=202,
    response_model=APIResponse[NavigationMutationEnqueueResultDTO],
)
async def enqueue_session_catalog_operations(
    payload: NavigationMutationEnqueueRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    """typed 导航 operation 批量入队：202 只表示 durable acceptance。"""
    try:
        result = await service.submit_operation_batch(payload, _navigation_scope(service))
    except NavigationBackpressureError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except NavigationQueueOwnerError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except NavigationMutationConflictError as error:
        raise state_conflict_http_error(error) from error
    except (KeyError, ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=client_error_message(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/operations",
    response_model=APIResponse[NavigationMutationStatusPageDTO],
)
async def get_session_catalog_operation_status(
    operation_id: list[str] = Query(min_length=1),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    """按精确 operation ID 返回 durable 状态；未知 ID 显式回报。"""
    try:
        result = service.operation_status(operation_id, _navigation_scope(service))
    except NavigationQueueOwnerError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (KeyError, ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=client_error_message(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/snapshot",
    response_model=APIResponse[NavigationSnapshotDTO],
)
async def get_session_catalog_snapshot(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    """revision-pinned catalog snapshot：同一只读事务返回 revision 与事件水位。"""
    try:
        result = service.navigation_snapshot()
    except NavigationQueueOwnerError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/navigation-events",
    response_model=APIResponse[NavigationEventsPageDTO],
)
async def list_session_catalog_navigation_events(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    """独立 navigation channel 的终态事件页（cursor 可恢复）。"""
    try:
        if cursor is not None:
            after, _watermark = service.decode_navigation_events_cursor(cursor)
        result = service.navigation_events(after=after, limit=limit)
    except NavigationQueueOwnerError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (ValueError, TypeError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-generations/capabilities",
    response_model=APIResponse[SessionGenerationCapabilitiesDTO],
)
async def list_session_generation_capabilities(
    request_id: str = Depends(get_request_id),
    service: SessionGenerationService = Depends(get_session_generation_service),
):
    return APIResponse(data=service.list_capabilities(), request_id=request_id)


@router.post(
    "/session-generations/preview-placement",
    response_model=APIResponse[SessionGenerationPreviewDTO],
)
async def preview_session_generation(
    payload: SessionGenerationPreviewRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[SessionGenerationService, Depends(get_session_generation_service)],
):
    try:
        result = await service.preview(payload)
    except KeyError as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=client_error_message(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-generations/status",
    response_model=APIResponse[SessionGenerationExecuteResultDTO],
)
async def get_session_generation_status(
    generator_id: str = Query(min_length=1),
    idempotency_key: str = Query(min_length=1),
    request_id: str = Depends(get_request_id),
    service: SessionGenerationService = Depends(get_session_generation_service),
):
    try:
        result = service.get_run_status(
            generator_id=generator_id,
            idempotency_key=idempotency_key,
        )
    except KeyError as error:
        raise not_found_http_error(error) from error
    except ClientStateConflictError as error:
        # 类型化后：失败运行缺 result 是客户端可查询/可恢复的状态冲突。
        raise state_conflict_error(error) from error
    except RuntimeError as error:
        # [过渡期] 受保护文件 session_generation/service.py 仍抛裸 RuntimeError，
        # 且其中 :369 同一行同时承载「failed 运行缺 result」（客户端可触发、必须 409）
        # 与「损坏的 completed 记录」（服务端完整性故障、应为 5xx），**类型上不可区分**。
        # 在受保护文件授权改动前，只能整体维持 409 以守住类A 的零回归红线。
        # TODO(授权后): 删除本分支，让完整性故障冒泡成 5xx。
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/children",
    response_model=APIResponse[SessionCatalogPageDTO],
)
async def list_session_catalog_children(
    parent_node_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    try:
        result = await service.list_children(
            parent_node_id=parent_node_id,
            limit=limit,
            cursor=cursor,
        )
    except KeyError as error:
        raise not_found_http_error(error) from error
    except (TypeError, ValueError, RuntimeError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/roots",
    response_model=APIResponse[SessionCatalogPageDTO],
)
async def list_session_catalog_roots(
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    try:
        result = await service.list_children(
            parent_node_id=None,
            limit=limit,
            cursor=cursor,
        )
    except (TypeError, ValueError, RuntimeError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/session-catalog/refresh",
    response_model=APIResponse[SessionCatalogPageDTO],
)
async def refresh_session_catalog(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    try:
        result = await service.refresh()
    except RuntimeError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/breadcrumb/{node_id}",
    response_model=APIResponse[SessionCatalogBreadcrumbDTO],
)
async def get_session_catalog_breadcrumb(
    node_id: str,
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    try:
        result = await service.breadcrumb(node_id)
    except KeyError as error:
        raise not_found_http_error(error) from error
    except RuntimeError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/export",
    response_model=APIResponse[SessionCatalogExportDTO],
)
async def export_session_catalog(
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    try:
        result = await service.export_index()
    except RuntimeError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/search",
    response_model=APIResponse[SessionCatalogSearchResultsDTO],
)
async def search_session_catalog(
    query: str = Query(min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
    request_id: str = Depends(get_request_id),
    service: SessionCatalogService = Depends(get_session_catalog_service),
):
    try:
        result = await service.search(query=query, limit=limit, cursor=cursor)
    except (TypeError, ValueError, RuntimeError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/session-generations/execute",
    response_model=APIResponse[SessionGenerationExecuteResultDTO],
)
async def execute_session_generation(
    payload: SessionGenerationExecuteRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: SessionGenerationService = Depends(get_session_generation_service),
):
    try:
        result = await service.execute(payload)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=client_error_message(error)) from error
    except ClientStateConflictError as error:
        # 类型化后：同幂等键撞上既有未完成/失败运行是客户端可恢复的状态冲突。
        raise state_conflict_error(error) from error
    except RuntimeError as error:
        # [过渡期] 受保护文件 session_generation/service.py 仍抛裸 RuntimeError：
        # :207-212 幂等键冲突（客户端可触发、必须 409）与 :201-206/:213-215/:642
        # 完整性故障（generator_id 不匹配、缺 result、账本非对象）**类型上不可区分**。
        # 在受保护文件授权改动前，只能整体维持 409 以守住类A 的零回归红线。
        # TODO(授权后): 删除本分支，让完整性故障冒泡成 5xx。
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)
