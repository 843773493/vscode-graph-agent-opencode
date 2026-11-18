from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry
from app.gateway.runtime.port_forwarding import PortForwardManager, port_forward_failure
from app.gateway.runtime.port_forwarding.hosts import list_hosts
from app.schemas.gateway.port_forwarding import (
    CreatePortForwardRequest,
    PortForwardHostsDTO,
    PortForwardListDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(tags=["gateway-port-forwards"], dependencies=[Depends(verify_gateway_token)])
logger = logging.getLogger(__name__)


def get_port_forward_manager(request: Request) -> PortForwardManager:
    manager = getattr(request.app.state, "port_forward_manager", None)
    if not isinstance(manager, PortForwardManager):
        raise TypeError("Gateway 端口转发管理器尚未初始化")
    return manager


def port_forward_http_error(
    error: Exception, *, stage: str = "request", permission_status_code: int = 502,
) -> HTTPException:
    logger.exception("Gateway SSH 端口转发请求失败: stage=%s", stage)
    detail = port_forward_failure(error, stage=stage)
    if isinstance(error, PermissionError):
        return HTTPException(status_code=permission_status_code, detail=detail)
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=detail)
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=detail)
    return HTTPException(status_code=502, detail=detail)


@router.get("/api/gateway/port-forwards/hosts", response_model=APIResponse[PortForwardHostsDTO])
async def port_forward_hosts(
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: str = Depends(get_request_id),
):
    try:
        hosts = await asyncio.to_thread(list_hosts, registry)
    except Exception as error:
        raise port_forward_http_error(error, stage="hosts") from error
    return APIResponse(data=PortForwardHostsDTO(gateway_id=registry.gateway_id, items=hosts), request_id=request_id)


@router.get("/api/gateway/port-forwards", response_model=APIResponse[PortForwardListDTO])
async def list_port_forwards(
    manager: Annotated[PortForwardManager, Depends(get_port_forward_manager)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: str = Depends(get_request_id),
):
    return APIResponse(data=PortForwardListDTO(gateway_id=registry.gateway_id, items=await manager.list()), request_id=request_id)


@router.post("/api/gateway/port-forwards", response_model=APIResponse[PortForwardListDTO])
async def create_port_forward(
    payload: CreatePortForwardRequest,
    manager: Annotated[PortForwardManager, Depends(get_port_forward_manager)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: str = Depends(get_request_id),
):
    try:
        await manager.create(payload)
    except Exception as error:
        raise port_forward_http_error(error) from error
    return APIResponse(data=PortForwardListDTO(gateway_id=registry.gateway_id, items=await manager.list()), request_id=request_id)


@router.put("/api/gateway/port-forwards/{forward_id}", response_model=APIResponse[PortForwardListDTO])
async def update_port_forward(
    forward_id: str, payload: CreatePortForwardRequest,
    manager: Annotated[PortForwardManager, Depends(get_port_forward_manager)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: str = Depends(get_request_id),
):
    try:
        await manager.update(forward_id, payload)
    except Exception as error:
        raise port_forward_http_error(error) from error
    return APIResponse(data=PortForwardListDTO(gateway_id=registry.gateway_id, items=await manager.list()), request_id=request_id)


@router.post("/api/gateway/port-forwards/{forward_id}/start", response_model=APIResponse[PortForwardListDTO])
async def start_port_forward(
    forward_id: str,
    manager: Annotated[PortForwardManager, Depends(get_port_forward_manager)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: str = Depends(get_request_id),
):
    try:
        await manager.set_running(forward_id, True)
    except Exception as error:
        raise port_forward_http_error(error) from error
    return APIResponse(data=PortForwardListDTO(gateway_id=registry.gateway_id, items=await manager.list()), request_id=request_id)


@router.post("/api/gateway/port-forwards/{forward_id}/stop", response_model=APIResponse[PortForwardListDTO])
async def stop_port_forward(
    forward_id: str,
    manager: Annotated[PortForwardManager, Depends(get_port_forward_manager)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: str = Depends(get_request_id),
):
    try:
        await manager.set_running(forward_id, False)
    except Exception as error:
        raise port_forward_http_error(error) from error
    return APIResponse(data=PortForwardListDTO(gateway_id=registry.gateway_id, items=await manager.list()), request_id=request_id)


@router.delete("/api/gateway/port-forwards/{forward_id}", response_model=APIResponse[PortForwardListDTO])
async def delete_port_forward(
    forward_id: str,
    manager: Annotated[PortForwardManager, Depends(get_port_forward_manager)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: str = Depends(get_request_id),
):
    try:
        await manager.delete(forward_id)
    except Exception as error:
        raise port_forward_http_error(error) from error
    return APIResponse(data=PortForwardListDTO(gateway_id=registry.gateway_id, items=await manager.list()), request_id=request_id)
