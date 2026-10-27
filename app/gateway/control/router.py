from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.generator_import import import_session_generators
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.credentials import FederationCredentialStore
from app.gateway.diagnostics import collect_gateway_diagnostics
from app.gateway.federation import request_remote_gateway_management
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.gateway.diagnostics import GatewayDiagnosticsDTO
from app.schemas.gateway_control import (
    GatewayResourceListDTO,
    GatewaySessionSearchResultsDTO,
    WorkspaceFolderCreateRequest,
    WorkspaceNavigationBreadcrumbDTO,
    WorkspaceNavigationNodeUpdateRequest,
    WorkspaceNavigationPlacementRequest,
    WorkspaceNavigationReorderRequest,
    WorkspaceNavigationTreeDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(prefix="/api/gateway", tags=["gateway-control"])


def _registry(request: Request) -> GatewayWorkspaceRegistry:
    value = getattr(request.app.state, "registry", None)
    if not isinstance(value, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return value


def _navigation_store(request: Request) -> WorkspaceNavigationStore:
    value = getattr(request.app.state, "workspace_navigation_store", None)
    if not isinstance(value, WorkspaceNavigationStore):
        raise RuntimeError("Gateway 工作区导航存储尚未初始化")
    return value


def _catalog_search(request: Request) -> GatewaySessionCatalogSearchService:
    value = getattr(request.app.state, "session_catalog_search_service", None)
    if not isinstance(value, GatewaySessionCatalogSearchService):
        raise RuntimeError("Gateway 跨工作区会话搜索服务尚未初始化")
    return value


def _resource_catalog(request: Request) -> GatewayResourceCatalogService:
    value = getattr(request.app.state, "gateway_resource_catalog_service", None)
    if not isinstance(value, GatewayResourceCatalogService):
        raise RuntimeError("Gateway 全局资源目录服务尚未初始化")
    return value


@router.get(
    "/resources",
    response_model=APIResponse[GatewayResourceListDTO],
    summary="聚合所有 Gateway 工作区的可连接资源",
)
async def list_gateway_resources(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: GatewayResourceCatalogService = Depends(_resource_catalog),
):
    return APIResponse(
        data=await service.list(request_id=request_id),
        request_id=request_id,
    )


@router.get(
    "/diagnostics",
    response_model=APIResponse[GatewayDiagnosticsDTO],
    summary="查看 Gateway 与托管工作区日志诊断",
)
async def get_gateway_diagnostics(
    gateway_connection_id: str | None = Query(default=None),
    workspace_id: str | None = Query(default=None),
    log_id: str | None = Query(default=None),
    tail_lines: int = Query(default=300, ge=20, le=1000),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
):
    if gateway_connection_id is None:
        try:
            result = await collect_gateway_diagnostics(
                registry,
                selected_workspace_id=workspace_id,
                selected_log_id=log_id,
                tail_lines=tail_lines,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return APIResponse(data=result, request_id=request_id)

    try:
        connection = registry.remote_gateway_connection(gateway_connection_id)
        remote_workspace_id: str | None = None
        if workspace_id is not None:
            target = registry.resolve(workspace_id)
            if (
                target.connection_kind != "remote_gateway"
                or target.remote_gateway_connection_id != gateway_connection_id
                or target.remote_workspace_id is None
            ):
                raise ValueError("所选工作区不属于当前远程 Gateway")
            remote_workspace_id = target.remote_workspace_id
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(gateway_connection_id)
        params = {
            "tail_lines": str(tail_lines),
            **(
                {"remote_workspace_id": remote_workspace_id}
                if remote_workspace_id is not None
                else {}
            ),
            **({"log_id": log_id} if log_id is not None else {}),
        }
        remote_data = await request_remote_gateway_management(
            gateway_url=registry.remote_gateway_url(gateway_connection_id),
            credential=credential,
            method="GET",
            path=f"/api/gateway/federation/diagnostics?{urlencode(params)}",
            request_id=request_id,
        )
        result = GatewayDiagnosticsDTO.model_validate(remote_data).model_copy(
            update={
                "gateway_connection_id": gateway_connection_id,
                "gateway_name": connection.name,
                "connection_kind": "remote_gateway",
                "selected_workspace_id": workspace_id,
            }
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "远程 Gateway 拒绝日志诊断请求: "
                f"status={error.response.status_code}, "
                f"body={error.response.text[:1000]}"
            ),
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"无法连接远程 Gateway: {error}",
        ) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/session-catalog/search",
    response_model=APIResponse[GatewaySessionSearchResultsDTO],
)
async def search_all_workspace_sessions(
    query: str,
    limit_per_workspace: int = 50,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: GatewaySessionCatalogSearchService = Depends(_catalog_search),
):
    if not 1 <= limit_per_workspace <= 200:
        raise HTTPException(status_code=400, detail="limit_per_workspace 必须在 1..200")
    try:
        result = await service.search(
            query,
            limit_per_workspace=limit_per_workspace,
            request_id=request_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/workspace-navigation",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def list_workspace_navigation(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    return APIResponse(
        data=store.list_tree(registry.targets()),
        request_id=request_id,
    )


@router.post(
    "/workspace-navigation/folders",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def create_workspace_folder(
    payload: WorkspaceFolderCreateRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.create_folder(payload, registry.targets())
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/workspace-navigation/nodes/{node_id}",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def update_workspace_navigation_node(
    node_id: str,
    payload: WorkspaceNavigationNodeUpdateRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.update_node(node_id, payload, registry.targets())
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/workspace-navigation/order",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def reorder_workspace_navigation(
    payload: WorkspaceNavigationReorderRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.reorder(payload, registry.targets())
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/workspace-navigation/placement",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def place_workspace_navigation_node(
    payload: WorkspaceNavigationPlacementRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.place(payload, registry.targets())
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/workspace-navigation/folders/{node_id}",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def delete_workspace_folder(
    node_id: str,
    recursive: bool = Query(default=False),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.delete_folder(
            node_id,
            registry.targets(),
            recursive=recursive,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/workspace-navigation/nodes/{node_id}/breadcrumb",
    response_model=APIResponse[WorkspaceNavigationBreadcrumbDTO],
)
async def get_workspace_navigation_breadcrumb(
    node_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(_registry),
    store: WorkspaceNavigationStore = Depends(_navigation_store),
):
    try:
        result = store.breadcrumb(node_id, registry.targets())
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/migrations/session-generators", response_model=APIResponse[list[dict[str, str]]])
async def migrate_session_generators(
    request: Request,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(_registry)],
    api: Annotated[GatewayResourceCatalogService, Depends(_resource_catalog)],
    preview: Annotated[bool, Query()] = True,
):
    result = await import_session_generators(
        root=get_gateway_root(), registry=registry, api=api, http_client=request.app.state.http_client, gateway_id=request.app.state.gateway_id,
        request_id=request_id, preview=preview,
    )
    return APIResponse(data=result, request_id=request_id)
