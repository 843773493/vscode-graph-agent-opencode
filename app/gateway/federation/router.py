"""``/api/gateway/federation/channel`` 的 FastAPI 路由。

路由只做依赖装配：从 ``app.state`` 取得已构造好的联邦服务与凭据存储，再把连接
交给 :func:`serve_federation_channel`。鉴权、握手与失败语义全部由 federation
包实现，路由层不重复一份 token 校验。
"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket

from app.core.trace_middleware import require_current_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.endpoint import (
    FederationChannelServer,
    serve_federation_channel,
)
from app.gateway.federation.errors import FederationError
from app.gateway.federation.grants import GrantTarget
from app.gateway.federation.messages import FEDERATION_DEADLINE_SECONDS
from app.gateway.federation.rpc import FederationRpcService
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.communication import (
    CommunicationDiscoveryRequest,
    CommunicationGatewayRequest,
    CommunicationObservationRequest,
)

router = APIRouter(prefix="/api/gateway/federation", tags=["gateway-federation"])


def _federation_service(websocket: WebSocket) -> FederationRpcService:
    value = getattr(websocket.app.state, "federation_rpc_service", None)
    if not isinstance(value, FederationRpcService):
        raise RuntimeError(  # noqa: TRY004 —— 运行时装配错误，非参数类型错误
            "Gateway 联邦 RPC 服务尚未初始化"
        )
    return value


def _credential_store(websocket: WebSocket) -> FederationCredentialStore:
    value = getattr(websocket.app.state, "federation_credential_store", None)
    if not isinstance(value, FederationCredentialStore):
        raise RuntimeError(  # noqa: TRY004 —— 运行时装配错误，非参数类型错误
            "Gateway 联邦凭据存储尚未初始化"
        )
    return value


@router.websocket("/channel")
async def federation_channel(websocket: WebSocket) -> None:
    service = _federation_service(websocket)
    await serve_federation_channel(
        websocket=websocket,
        server=FederationChannelServer(
            local_gateway_id=service.gateway_id,
            gateway_root=websocket.app.state.federation_gateway_root,
            credential_store=_credential_store(websocket),
            handler=service.request_handler(),
            local_role=service.local_role,
            on_channel_ready=service.register_channel,
            on_channel_closed=service.unregister_channel,
        ),
    )


__all__ = ["router"]


@router.post("/discovery")
async def discover_communication_target(payload: CommunicationDiscoveryRequest, request: Request, _: Annotated[str, Depends(verify_gateway_token)]):
    service = request.app.state.federation_rpc_service
    try:
        service.session_main.require_local_source(service.gateway_id, asdict(payload.source))
        workspace = payload.workspace_id
        if workspace is not None and payload.gateway_id in {None, service.gateway_id}:
            workspace = service.session_main.canonical_workspace_id(workspace)
        result = await service.resolve_session_target(session_id=payload.session_id, workspace_id=workspace, gateway_id=payload.gateway_id)
    except FederationError as error:
        raise HTTPException(status_code=409, detail=error.to_wire()) from error
    return APIResponse(data={"target": {key: result[key] for key in ("gateway_id", "workspace_id", "session_id", "thread_id")}, "main_thread_id": result["main_thread_id"]}, request_id=require_current_request_id("discovery response"))


@router.post("/relay")
async def relay_communication(payload: CommunicationGatewayRequest, request: Request, _: Annotated[str, Depends(verify_gateway_token)]):
    service = request.app.state.federation_rpc_service
    try:
        if payload.fork_target is not None:
            if payload.body.get("source_gateway_id") != service.gateway_id:
                raise ValueError("fork relay origin 不是当前 Gateway")
            target = payload.fork_target
        elif payload.navigation_target is not None:
            if payload.body.get("source_gateway_id") != service.gateway_id:
                raise ValueError("资源导航 origin 不是当前 Gateway")
            target = payload.navigation_target
        elif payload.resource_target is not None:
            if payload.body.get("source_gateway_id") != service.gateway_id:
                raise ValueError("上下文查询 origin 不是当前 Gateway")
            target = payload.resource_target
        else:
            source = payload.body.get("source")
            if not isinstance(source, dict):
                raise TypeError("通信 relay 缺少 source")
            service.session_main.require_local_source(service.gateway_id, source)
            target = GrantTarget(payload.target.gateway_id, payload.target.workspace_id, payload.target.session_id)
        timeout = FEDERATION_DEADLINE_SECONDS
        if payload.operation == "wait":
            observation = CommunicationObservationRequest.model_validate(payload.body)
            deadline = observation.query["deadline"]["deadline_at_utc"]
            timeout = max((datetime.fromisoformat(deadline) - datetime.now(UTC)).total_seconds(), 0) + 10
        result = await service.relay_operation(target=target, operation=payload.operation, body=payload.body, deadline_at=time.time() + timeout)
        while isinstance(result.get("result"), dict) and "relayed_by" in result:
            result = result["result"]
    except FederationError as error:
        raise HTTPException(status_code=403 if "authorization" in error.code else 409, detail=error.to_wire()) from error
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=require_current_request_id("relay response"))
