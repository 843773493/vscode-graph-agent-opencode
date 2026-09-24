"""``/api/gateway/federation/channel`` 的 FastAPI 路由。

路由只做依赖装配：从 ``app.state`` 取得已构造好的联邦服务与凭据存储，再把连接
交给 :func:`serve_federation_channel`。鉴权、握手与失败语义全部由 federation
包实现，路由层不重复一份 token 校验。
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.endpoint import (
    FederationChannelServer,
    serve_federation_channel,
)
from app.gateway.federation.rpc import FederationRpcService

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
