from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.core.failures import failure_detail
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.control.generator_import import import_session_generators
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.control.workspace_discovery import discover_workspaces
from app.gateway.credentials import FederationCredentialStore
from app.gateway.diagnostics import collect_gateway_diagnostics
from app.gateway.federation import request_remote_gateway_management
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER, parse_target_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry, get_workspace_navigation_store
from app.schemas.gateway.diagnostics import GatewayDiagnosticsDTO
from app.schemas.gateway.search import GatewayWorkspaceDiscoveryResultsDTO
from app.schemas.gateway_control import (
    GatewayResourceListDTO,
    WorkspaceFolderCreateRequest,
    WorkspaceNavigationBreadcrumbDTO,
    WorkspaceNavigationNodeUpdateRequest,
    WorkspaceNavigationPlacementRequest,
    WorkspaceNavigationReorderRequest,
    WorkspaceNavigationTreeDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(prefix="/api/gateway", tags=["gateway-control"])


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
    request: Request,
    gateway_connection_id: str | None = Query(default=None),
    workspace_id: str | None = Query(default=None),
    log_id: str | None = Query(default=None),
    tail_lines: int = Query(default=300, ge=20, le=1000),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    target_gateway_id: Annotated[str | None, Header(alias=TARGET_GATEWAY_ID_HEADER)] = None,
):
    target_gateway_id = parse_target_gateway_id(target_gateway_id)
    if gateway_connection_id is None and workspace_id is not None:
        try:
            target = registry.resolve_request_target(workspace_id, target_gateway_id)
            if target.connection_kind == "remote_gateway":
                gateway_connection_id = registry.remote_connection_for_target(target).connection_id
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    if gateway_connection_id is None and target_gateway_id is not None and target_gateway_id != registry.gateway_id:
        raise HTTPException(status_code=400, detail="远端 Gateway 诊断需要明确连接或工作区归属")
    if gateway_connection_id is None:
        try:
            result = await collect_gateway_diagnostics(
                registry,
                selected_workspace_id=workspace_id,
                selected_log_id=log_id,
                tail_lines=tail_lines,
                service_plugins=getattr(request.app.state, "service_plugins", None),
                observation=getattr(request.app.state, "runtime_observation", None),
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return APIResponse(data=result, request_id=request_id)

    try:
        connection = registry.remote_gateway_connection(gateway_connection_id)
        if target_gateway_id is not None and target_gateway_id != connection.remote_gateway_id:
            raise ValueError("目标 Gateway 与所选诊断连接归属不一致")
        if workspace_id is not None:
            target = registry.resolve((connection.remote_gateway_id, workspace_id))
            if (
                target.connection_kind != "remote_gateway"
                or gateway_connection_id not in target.remote_gateway_connection_ids
            ):
                raise ValueError("所选工作区不属于当前远程 Gateway")
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(gateway_connection_id)
        params = {
            "tail_lines": str(tail_lines),
            **(
                {"workspace_id": workspace_id}
                if workspace_id is not None
                else {}
            ),
            **({"log_id": log_id} if log_id is not None else {}),
        }
        gateway_url = registry.remote_gateway_url(gateway_connection_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    try:
        remote_data = await request_remote_gateway_management(
            gateway_url=gateway_url,
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
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=failure_detail(error, stage="diagnostics_response", label="远程 Gateway 诊断响应无效"),
        ) from error
    except httpx.HTTPStatusError as error:
        # 原始拒绝正文只留诊断；它不属于成功回包中具名的原始日志字段。
        error.add_note(f"远程 Gateway 诊断拒绝正文: {error.response.text}")
        raise HTTPException(
            status_code=502,
            detail=failure_detail(
                error, stage="diagnostics_request",
                label=f"远程 Gateway 拒绝日志诊断请求: status={error.response.status_code}",
            ),
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=failure_detail(error, stage="diagnostics_request", label="无法连接远程 Gateway"),
        ) from error
    return APIResponse(data=result, request_id=request_id)


@router.get("/search/workspaces", response_model=APIResponse[GatewayWorkspaceDiscoveryResultsDTO])
async def discover_gateway_workspaces(
    request: Request,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    store: Annotated[WorkspaceNavigationStore, Depends(get_workspace_navigation_store)],
    query: Annotated[str, Query(max_length=512)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=8192)] = None,
    include_folders: bool = False,
):
    try:
        result = discover_workspaces(
            registry, store, gateway_id=request.app.state.gateway_id,
            query=query, limit=limit, cursor=cursor, include_folders=include_folders,
        )
    except (ValueError, LookupError) as error:
        raise HTTPException(409, detail={"owner": "workspace_discovery", "message": str(error)}) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/workspace-navigation",
    response_model=APIResponse[WorkspaceNavigationTreeDTO],
)
async def list_workspace_navigation(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    store: WorkspaceNavigationStore = Depends(get_workspace_navigation_store),
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
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    store: WorkspaceNavigationStore = Depends(get_workspace_navigation_store),
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
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    store: WorkspaceNavigationStore = Depends(get_workspace_navigation_store),
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
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    store: WorkspaceNavigationStore = Depends(get_workspace_navigation_store),
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
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    store: WorkspaceNavigationStore = Depends(get_workspace_navigation_store),
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
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    store: WorkspaceNavigationStore = Depends(get_workspace_navigation_store),
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
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    store: WorkspaceNavigationStore = Depends(get_workspace_navigation_store),
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
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    api: Annotated[GatewayResourceCatalogService, Depends(_resource_catalog)],
    preview: Annotated[bool, Query()] = True,
):
    result = await import_session_generators(
        root=get_gateway_root(), registry=registry, api=api, http_client=request.app.state.http_client, gateway_id=request.app.state.gateway_id,
        request_id=request_id, preview=preview,
    )
    return APIResponse(data=result, request_id=request_id)
