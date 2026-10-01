"""Gateway 托管工作区注册与入站访问路由。"""

from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)

from app.core.env import get_project_root
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
from app.gateway.remote_gateway import refresh_remote_gateway_projections
from app.gateway.routes._shared import (
    _gateway_root,
    _managed_workspace_list,
    get_registry,
)
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.server.port_forwarding import get_port_forward_manager
from app.schemas.gateway import (
    CreateGatewayManagedWorkspaceRequest,
    GatewayInboundAccessListDTO,
    GatewayInboundPeerDTO,
    GatewayInboundWorkspaceDTO,
    GatewayManagedWorkspaceListDTO,
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


def _remote_http_error_detail(error: httpx.HTTPStatusError) -> str:
    try:
        payload = error.response.json()
    except ValueError:
        return error.response.text[:1000]
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return error.response.text[:1000]


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
            root_path=workspace.root_path,
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
            raise HTTPException(status_code=404, detail=str(error)) from error
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            raise HTTPException(
                status_code=status_code if 400 <= status_code < 500 else 502,
                detail=_remote_http_error_detail(error),
            ) from error
        except (PermissionError, RuntimeError, httpx.HTTPError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
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
                log_dir=_gateway_root() / "logs",
                root_path=payload.root_path,
                name=payload.name,
                create_directory=payload.create_directory,
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
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=_remote_http_error_detail(error),
        ) from error
    except (PermissionError, OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
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
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    remote_data: dict[str, object] | None = None
    try:
        if gateway_connection_id is None:
            remove_direct_managed_workspace(registry, workspace_id)
        else:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="DELETE",
                path=(
                    "/api/gateway/federation/managed-workspaces/"
                    f"{quote(workspace_id, safe='')}"
                ),
                request_id=request_id,
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=gateway_connection_id,
            )
            await port_forward_manager.reconcile_workspaces()
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=_remote_http_error_detail(error),
        ) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
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
