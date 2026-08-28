from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.schemas.gateway import (
    ChangePortForwardLabelRequest,
    ChangePortForwardLocalPortRequest,
    CreatePortForwardRequest,
    PortForwardListDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(tags=["gateway-port-forwards"])


def get_port_forward_manager(request: Request) -> SshPortForwardManager:
    manager = getattr(request.app.state, "port_forward_manager", None)
    if not isinstance(manager, SshPortForwardManager):
        raise TypeError("Gateway SSH 端口转发管理器尚未初始化")
    return manager


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=502, detail=str(error))


@router.get(
    "/api/gateway/workspaces/{workspace_id}/port-forwards",
    response_model=APIResponse[PortForwardListDTO],
)
async def list_port_forwards(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    manager: SshPortForwardManager = Depends(get_port_forward_manager),  # noqa: B008
):
    try:
        items = await manager.list(workspace_id)
    except Exception as error:
        raise _http_error(error) from error
    return APIResponse(data=PortForwardListDTO(items=items), request_id=request_id)


@router.post(
    "/api/gateway/workspaces/{workspace_id}/port-forwards",
    response_model=APIResponse[PortForwardListDTO],
)
async def create_port_forward(
    workspace_id: str,
    payload: CreatePortForwardRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    manager: SshPortForwardManager = Depends(get_port_forward_manager),  # noqa: B008
):
    try:
        await manager.create(workspace_id, payload)
        items = await manager.list(workspace_id)
    except Exception as error:
        raise _http_error(error) from error
    return APIResponse(data=PortForwardListDTO(items=items), request_id=request_id)


@router.delete(
    "/api/gateway/workspaces/{workspace_id}/port-forwards/{forward_id}",
    response_model=APIResponse[PortForwardListDTO],
)
async def delete_port_forward(
    workspace_id: str,
    forward_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    manager: SshPortForwardManager = Depends(get_port_forward_manager),  # noqa: B008
):
    try:
        await manager.delete(workspace_id, forward_id)
        items = await manager.list(workspace_id)
    except Exception as error:
        raise _http_error(error) from error
    return APIResponse(data=PortForwardListDTO(items=items), request_id=request_id)


@router.post(
    "/api/gateway/workspaces/{workspace_id}/port-forwards/{forward_id}/reconnect",
    response_model=APIResponse[PortForwardListDTO],
)
async def reconnect_port_forward(
    workspace_id: str,
    forward_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    manager: SshPortForwardManager = Depends(get_port_forward_manager),  # noqa: B008
):
    try:
        await manager.reconnect(workspace_id, forward_id)
        items = await manager.list(workspace_id)
    except Exception as error:
        raise _http_error(error) from error
    return APIResponse(data=PortForwardListDTO(items=items), request_id=request_id)


@router.patch(
    "/api/gateway/workspaces/{workspace_id}/port-forwards/{forward_id}/local-port",
    response_model=APIResponse[PortForwardListDTO],
)
async def change_local_port(
    workspace_id: str,
    forward_id: str,
    payload: ChangePortForwardLocalPortRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    manager: SshPortForwardManager = Depends(get_port_forward_manager),  # noqa: B008
):
    try:
        await manager.change_local_port(workspace_id, forward_id, payload.local_port)
        items = await manager.list(workspace_id)
    except Exception as error:
        raise _http_error(error) from error
    return APIResponse(data=PortForwardListDTO(items=items), request_id=request_id)


@router.patch(
    "/api/gateway/workspaces/{workspace_id}/port-forwards/{forward_id}/label",
    response_model=APIResponse[PortForwardListDTO],
)
async def change_port_forward_label(
    workspace_id: str,
    forward_id: str,
    payload: ChangePortForwardLabelRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    manager: SshPortForwardManager = Depends(get_port_forward_manager),  # noqa: B008
):
    try:
        await manager.change_label(workspace_id, forward_id, payload.label)
        items = await manager.list(workspace_id)
    except Exception as error:
        raise _http_error(error) from error
    return APIResponse(data=PortForwardListDTO(items=items), request_id=request_id)
