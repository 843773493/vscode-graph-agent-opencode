"""Gateway 联邦清单、诊断与联邦托管工作区路由。"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)

from app.core.env import get_project_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_federation_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.diagnostics import collect_gateway_diagnostics
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    remove_direct_managed_workspace,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import (
    _directory_listing,
    _gateway_root,
    _managed_workspace_list,
    get_gateway_config_reload_service,
    get_registry,
)
from app.schemas.gateway import (
    CreateFederationManagedWorkspaceRequest,
    FederationProtocolManifestDTO,
    FederationWorkspaceDTO,
    FederationWorkspaceListDTO,
    GatewayDiagnosticsDTO,
    GatewayDirectoryListDTO,
    GatewayManagedWorkspaceListDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


@router.get(
    "/api/gateway/federation/manifest",
    response_model=APIResponse[FederationProtocolManifestDTO],
)
async def federation_manifest(
    request: Request,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
):
    gateway_state = getattr(request.app.state, "gateway_state", None)
    if not isinstance(gateway_state, GatewayStateStore):
        raise RuntimeError("Gateway state 尚未初始化")
    config_status = get_gateway_config_reload_service(request).status()
    _, config_event_cursor = gateway_state.config_event_bounds(config_domain="gateway")
    return APIResponse(
        data=FederationProtocolManifestDTO(
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
            capabilities=[
                "workspace_discovery",
                "workspace_proxy",
                "auxiliary_proxy",
                "diagnostics_logs",
                "managed_backend_restart",
                "managed_workspace_admin",
            ],
            config_event_cursor=config_event_cursor,
            config_reload_state=config_status.state,
            config_reload_restart_required=config_status.restart_required,
            config_reload_candidate_ref=config_status.candidate_ref,
        ),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/federation/workspaces",
    response_model=APIResponse[FederationWorkspaceListDTO],
)
async def federation_workspaces(
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    def services_for(workspace_id: str) -> list[str]:
        services = ["workspace_api"]
        for service, public_name in (
            ("terminal_manager", "terminal_manager"),
            ("browser_manager", "browser_manager"),
        ):
            try:
                registry.resolve_service_url(workspace_id, service)
            except LookupError:
                continue
            services.append(public_name)
        return services

    direct = [
        FederationWorkspaceDTO(
            workspace_id=target.workspace_id,
            name=target.name,
            root_path=target.root_path,
            managed=target.managed,
            connection_kind="local",
            services=services_for(target.workspace_id),
        )
        for target in registry.targets()
        if target.connection_kind == "local"
    ]
    excluded = [
        (f"{target.workspace_id}: bounded federation 不导出从其他 Gateway 导入的工作区")
        for target in registry.targets()
        if target.connection_kind == "remote_gateway"
    ]
    return APIResponse(
        data=FederationWorkspaceListDTO(
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
            items=direct,
            excluded=excluded,
        ),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/federation/diagnostics",
    response_model=APIResponse[GatewayDiagnosticsDTO],
)
async def federation_diagnostics(
    remote_workspace_id: str | None = Query(default=None),
    log_id: str | None = Query(default=None),
    tail_lines: int = Query(default=300, ge=20, le=1000),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        result = await collect_gateway_diagnostics(
            registry,
            selected_workspace_id=remote_workspace_id,
            selected_log_id=log_id,
            tail_lines=tail_lines,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/api/gateway/federation/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def federation_managed_workspaces(
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/federation/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def create_federation_managed_workspace(
    payload: CreateFederationManagedWorkspaceRequest,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        await create_direct_managed_workspace(
            registry=registry,
            project_root=get_project_root(),
            log_dir=_gateway_root() / "logs",
            root_path=payload.root_path,
            name=payload.name,
            create_directory=payload.create_directory,
        )
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@router.delete(
    "/api/gateway/federation/managed-workspaces/{workspace_id}",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def remove_federation_managed_workspace(
    workspace_id: str,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        remove_direct_managed_workspace(registry, workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/federation/directories",
    response_model=APIResponse[GatewayDirectoryListDTO],
)
async def list_federation_directories(
    path: str | None = Query(default=None),
    limit: int = Query(default=120, ge=1, le=500),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
):
    try:
        listing = await _directory_listing(path, limit=limit)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(
            status_code=400,
            detail=f"读取远程 Gateway 目录失败: {error}",
        ) from error
    return APIResponse(data=listing, request_id=request_id)
