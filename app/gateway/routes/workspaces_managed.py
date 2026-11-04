"""Gateway 托管工作区注册与入站访问路由。"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

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
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.credentials import (
    FederationCredential,
    FederationCredentialStore,
    load_or_create_gateway_id,
)
from app.gateway.federation import request_remote_gateway_management
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    remove_direct_managed_workspace,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.remote_gateway import (
    reconnect_remote_gateway,
    refresh_remote_gateway_projections,
)
from app.gateway.routes._shared import (
    _gateway_root,
    _managed_workspace_list,
    get_registry,
    get_workspace_runtime_controller,
    resolve_management_directory,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.server.port_forwarding import (
    get_port_forward_manager,
    port_forward_http_error,
)
from app.schemas.gateway.federation import (
    GatewayInboundAccessListDTO,
    GatewayInboundPeerDTO,
    GatewayInboundWorkspaceDTO,
)
from app.schemas.gateway.lifecycle import (
    GatewayRuntimeGroupRequest,
    GatewayRuntimeStateResultDTO,
)
from app.schemas.gateway.workspaces import (
    CreateGatewayManagedWorkspaceRequest,
    GatewayManagedWorkspaceListDTO,
    GatewayWorkspaceListDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def _remote_gateway_credential(connection_id: str) -> FederationCredential:
    return FederationCredentialStore(
        storage_path=_gateway_root() / "credentials" / "federation.json"
    ).get(connection_id)


def _remote_managed_workspace_list(
    registry: GatewayWorkspaceRegistry,
    connection_id: str,
    remote_data: dict[str, object],
) -> GatewayManagedWorkspaceListDTO:
    connection = registry.remote_gateway_connection(connection_id)
    remote_result = GatewayManagedWorkspaceListDTO.model_validate(remote_data)
    return remote_result.model_copy(
        update={
            "gateway_connection_id": connection_id,
            "gateway_name": connection.name,
            "connection_kind": "remote_gateway",
        }
    )


async def _inbound_gateway_access_list(
    registry: GatewayWorkspaceRegistry,
) -> GatewayInboundAccessListDTO:
    gateway_id = load_or_create_gateway_id(_gateway_root() / "identity.json")
    credentials = FederationCredentialStore(
        storage_path=_gateway_root() / "credentials" / "federation.json"
    ).list_valid()
    peers = [
        GatewayInboundPeerDTO(
            connection_id=credential.connection_id,
            peer_gateway_id=credential.peer_gateway_id,
            credential_expires_at=credential.expires_at.isoformat(),
        )
        for credential in credentials
        if credential.peer_gateway_id != gateway_id
    ]
    workspaces = [
        GatewayInboundWorkspaceDTO(
            workspace_id=workspace.workspace_id,
            name=workspace.name,
            status=workspace.status,
            managed=workspace.managed,
            system_default=workspace.system_default,
        )
        for workspace in await registry.list_dtos()
        if workspace.connection_kind == "local"
    ]
    return GatewayInboundAccessListDTO(
        gateway_id=gateway_id,
        peers=peers,
        items=workspaces if peers else [],
    )


@router.get(
    "/api/gateway/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def gateway_managed_workspaces(
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    if gateway_connection_id is not None:
        try:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="GET",
                path="/api/gateway/federation/managed-workspaces",
                request_id=request_id,
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=gateway_connection_id,
            )
            await port_forward_manager.reconcile_workspaces()
        except LookupError as error:
            raise HTTPException(status_code=404, detail=failure_detail(error, stage="workspace_list", label="工作区清单失败")) from error
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            raise HTTPException(
                status_code=status_code if 400 <= status_code < 500 else 502,
                detail=failure_detail(error, stage="workspace_list", label="工作区清单失败"),
            ) from error
        except (PermissionError, RuntimeError, httpx.HTTPError) as error:
            raise HTTPException(status_code=502, detail=failure_detail(error, stage="workspace_list", label="工作区清单失败")) from error
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                gateway_connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/inbound-access",
    response_model=APIResponse[GatewayInboundAccessListDTO],
)
async def gateway_inbound_access(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=await _inbound_gateway_access_list(registry),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def create_gateway_managed_workspace(
    payload: CreateGatewayManagedWorkspaceRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    connection_id = payload.gateway_connection_id
    remote_data: dict[str, object] | None = None
    try:
        if connection_id is None:
            await create_direct_managed_workspace(
                registry=registry,
                project_root=get_project_root(),
                log_dir=get_runtime_log_root(),
                root_path=str(resolve_management_directory(request, payload.root_path)),
                name=payload.name,
                create_directory=payload.create_directory,
                request_id=request_id,
            )
        else:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(connection_id),
                credential=_remote_gateway_credential(connection_id),
                method="POST",
                path="/api/gateway/federation/managed-workspaces",
                request_id=request_id,
                payload={
                    "root_path": payload.root_path,
                    "name": payload.name,
                    "create_directory": payload.create_directory,
                },
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=connection_id,
            )
            await port_forward_manager.reconcile_workspaces()
    except LookupError as error:
        raise HTTPException(status_code=404, detail=failure_detail(error, stage="workspace_create", label="工作区创建失败")) from error
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=failure_detail(error, stage="workspace_create", label="工作区创建失败")) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=failure_detail(error, stage="workspace_create", label="工作区创建失败"),
        ) from error
    except (PermissionError, OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=failure_detail(error, stage="workspace_create", label="工作区创建失败")) from error
    if connection_id is not None and remote_data is not None:
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@router.delete(
    "/api/gateway/managed-workspaces/{workspace_id}",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def remove_gateway_managed_workspace(
    workspace_id: str,
    payload: Annotated[GatewayRuntimeGroupRequest | None, Body()] = None,
    force: bool = False,
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: Annotated[GatewayWorkspaceRuntimeController, Depends(get_workspace_runtime_controller)] = None,
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    remote_data: dict[str, object] | None = None
    try:
        if gateway_connection_id is None:
            result = await remove_direct_managed_workspace(
                registry, workspace_id, controller=controller, request_id=request_id,
                group_request=payload, force=force,
            )
            if result.status == "blocked":
                raise HTTPException(status_code=409, detail=result.model_dump(mode="json"))
        else:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="DELETE",
                path=(
                    "/api/gateway/federation/managed-workspaces/"
                    f"{quote(workspace_id, safe='')}?force={'true' if force else 'false'}"
                ),
                payload=payload.model_dump(mode="json") if payload is not None else None,
                request_id=request_id, timeout_seconds=150,
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=gateway_connection_id,
            )
            await port_forward_manager.reconcile_workspaces()
    except LookupError as error:
        raise HTTPException(status_code=404, detail=failure_detail(error, stage="workspace_remove", label="工作区删除失败")) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=failure_detail(error, stage="workspace_remove", label="工作区删除失败")) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        detail: str | dict[str, object] = failure_detail(error, stage="workspace_remove", label="工作区删除失败")
        if status_code == 409:
            try:
                blocked = GatewayRuntimeStateResultDTO.model_validate(error.response.json()["detail"])
                if blocked.status != "blocked":
                    raise ValueError("远端删除409响应必须是blocked完整组回执")
            except (ValueError, KeyError, TypeError) as protocol_error:
                failure_detail(protocol_error, stage="workspace_remove_response", label="远端删除回执无效")
            else:
                detail = blocked.model_dump(mode="json")
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=detail,
        ) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=failure_detail(error, stage="workspace_remove", label="工作区删除失败")) from error
    if gateway_connection_id is not None and remote_data is not None:
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                gateway_connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@router.post("/api/gateway/remote-gateways/{connection_id}/reconnect", response_model=APIResponse[GatewayWorkspaceListDTO])
async def reconnect_gateway_connection(
    connection_id: str,
    request: Request,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
):
    config = request.app.state.gateway_config
    try:
        await reconnect_remote_gateway(
            registry=registry, connection_id=connection_id, log_dir=get_runtime_log_root(),
            health_request_timeout_seconds=config.gateway_process_health_request_timeout_seconds,
            health_poll_interval_seconds=config.gateway_process_health_poll_interval_seconds,
        )
    except LookupError as error:
        raise HTTPException(404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(409, detail=str(error)) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(502, detail=failure_detail(error, stage="gateway_connection_reconnect", label="远端 Gateway 连接重连失败")) from error
    return APIResponse(data=GatewayWorkspaceListDTO(active_workspace=registry.active_workspace_dto, items=await registry.list_dtos()), request_id=request_id)


@router.delete("/api/gateway/remote-gateways/{connection_id}", response_model=APIResponse[GatewayWorkspaceListDTO])
async def remove_gateway_connection(
    connection_id: str,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    port_forward_manager: Annotated[SshPortForwardManager, Depends(get_port_forward_manager)],
):
    try:
        registry.validate_remote_gateway_removal(connection_id)
        try:
            await port_forward_manager.reconcile_workspaces(removing_connection_id=connection_id)
        except (OSError, RuntimeError) as error:
            raise port_forward_http_error(error, stage="gateway_connection_remove") from error
        registry.remove_remote_gateway(connection_id)
    except LookupError as error:
        raise HTTPException(404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, detail=str(error)) from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(409, detail=str(error)) from error
    return APIResponse(data=GatewayWorkspaceListDTO(active_workspace=registry.active_workspace_dto, items=await registry.list_dtos()), request_id=request_id)
