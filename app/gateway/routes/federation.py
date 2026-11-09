"""Gateway 联邦清单、诊断与联邦托管工作区路由。"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
)

from app.core.env import get_project_root
from app.core.failures import failure_detail
from app.core.logging_contract import get_runtime_log_root
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_federation_token,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.diagnostics import collect_gateway_diagnostics
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION
from app.gateway.federation.policy import require_federation_capability
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
    get_workspace_runtime_controller,
    resolve_management_directory,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.schemas.gateway.diagnostics import GatewayDiagnosticsDTO
from app.schemas.gateway.directory import GatewayDirectoryListDTO
from app.schemas.gateway.federation import (
    FederationProtocolManifestDTO,
    FederationWorkspaceDTO,
    FederationWorkspaceListDTO,
)
from app.schemas.gateway.lifecycle import GatewayRuntimeGroupRequest
from app.schemas.gateway.workspaces import (
    CreateFederationManagedWorkspaceRequest,
    GatewayManagedWorkspaceListDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def require_managed_workspace_admin(
    request: Request, auth: Annotated[GatewayAuthContext, Depends(verify_federation_token)],
) -> None:
    """目标 Gateway 在目录、registry 与 runtime 副作用前读取唯一 policy。"""
    if auth.kind != "federation" or not auth.peer_gateway_id:
        raise RuntimeError("federation 管理请求缺少认证 peer 身份")
    require_federation_capability(request, auth, "managed_workspace_admin")


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
    verify_identity: Annotated[bool, Query()] = False,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    if verify_identity:
        revision = registry.registry_revision
        await run_sync_shielded(registry.validate_local_root_bindings, get_project_root())
        if registry.registry_revision != revision:
            raise HTTPException(status_code=409, detail="原生工作区登记在身份核验期间变化，请重新预检")

    def services_for(workspace_id: str) -> list[str]:
        return ["workspace_api"]

    direct = []
    for target in registry.targets():
        if target.connection_kind != "local":
            continue
        backend = registry.backend_owner(target.workspace_id)
        direct.append(FederationWorkspaceDTO(
            workspace_id=target.workspace_id,
            backend_id=backend.backend_id if backend is not None else target.backend_id,
            backend_generation=backend.contract.generation if backend is not None else None,
            affected_workspace_ids=list(backend.contract.workspace_ids) if backend is not None else [target.workspace_id],
            name=target.name,
            managed=target.managed,
            connection_kind="local",
            services=services_for(target.workspace_id),
        ))
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
            identities_verified=verify_identity,
        ),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/federation/diagnostics",
    response_model=APIResponse[GatewayDiagnosticsDTO],
)
async def federation_diagnostics(
    request: Request,
    workspace_id: str | None = Query(default=None),
    log_id: str | None = Query(default=None),
    tail_lines: int = Query(default=300, ge=20, le=1000),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        result = await collect_gateway_diagnostics(
            registry,
            selected_workspace_id=workspace_id,
            selected_log_id=log_id,
            tail_lines=tail_lines,
            extension_management=getattr(request.app.state, "extension_management", None),
            service_plugins=getattr(request.app.state, "service_plugins", None),
            observation=getattr(request.app.state, "runtime_observation", None),
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
    request: Request,
    _: None = Depends(require_managed_workspace_admin),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        await create_direct_managed_workspace(
            registry=registry,
            project_root=get_project_root(),
            log_dir=get_runtime_log_root(),
            root_path=str(resolve_management_directory(request, payload.root_path)),
            name=payload.name,
            create_directory=payload.create_directory,
            request_id=request_id,
            config=request.app.state.gateway_config_reload,
            plugins=request.app.state.service_plugins,
        )
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=failure_detail(error, stage="workspace_create", label="工作区创建失败")) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=failure_detail(error, stage="workspace_create", label="工作区创建失败")) from error
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
    payload: Annotated[GatewayRuntimeGroupRequest | None, Body()] = None,
    force: bool = False,
    _: None = Depends(require_managed_workspace_admin),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: Annotated[GatewayWorkspaceRuntimeController, Depends(get_workspace_runtime_controller)] = None,
):
    try:
        result = await remove_direct_managed_workspace(
            registry, workspace_id, controller=controller, request_id=request_id,
            group_request=payload, force=force,
        )
        if result.status == "blocked":
            raise HTTPException(status_code=409, detail=result.model_dump(mode="json"))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=failure_detail(error, stage="workspace_remove", label="工作区删除失败")) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=failure_detail(error, stage="workspace_remove", label="工作区删除失败")) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=failure_detail(error, stage="workspace_remove", label="工作区删除失败")) from error
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/federation/directories",
    response_model=APIResponse[GatewayDirectoryListDTO],
)
async def list_federation_directories(
    request: Request,
    path: str | None = Query(default=None),
    limit: int = Query(default=120, ge=1, le=500),
    base_path: str | None = Query(default=None),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
):
    try:
        listing = await _directory_listing(request, path, limit=limit, base_path=base_path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=failure_detail(error, stage="directory_browse", label="目录浏览失败")) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=failure_detail(error, stage="directory_browse", label="目录浏览失败")) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=failure_detail(error, stage="directory_browse", label="目录浏览失败")) from error
    except OSError as error:
        raise HTTPException(
            status_code=400,
            detail=failure_detail(error, stage="directory_browse", label="目录浏览失败"),
        ) from error
    return APIResponse(data=listing, request_id=request_id)
