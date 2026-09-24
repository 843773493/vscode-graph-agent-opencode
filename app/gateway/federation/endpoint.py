"""``/api/gateway/federation/channel`` 的握手、心跳与请求分发（服务端）。

握手顺序固定：先用既有 federation token 认证对端（不新增第二套 token 校验），
再从 hello 帧把对端声明的 ``gateway_id``/公钥绑定到 token 已认证的
``peer_gateway_id``；绑定失败立即以显式 close code 拒绝，不做匿名降级。

WebSocket 必须先 ``accept`` 才能发送 close 帧，因此本模块先接受连接，随后在
任何鉴权/握手失败时用 1008 关闭并给出可诊断 reason。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from starlette.websockets import WebSocket

from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.federation.channel import (
    ChannelHandshake,
    FederationChannelSession,
    FederationRequestHandler,
    new_channel_instance_id,
    parse_hello_payload,
)
from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_INVALID_CREDENTIAL,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_MISSING_CREDENTIAL,
    FederationError,
)
from app.gateway.federation.identity import (
    load_or_create_signing_key,
    peer_identity_from_hello,
    public_key_pem,
)
from app.gateway.federation.protocol import (
    FRAME_HELLO,
    FRAME_WELCOME,
    FederationFrame,
)
from app.gateway.federation.transport import FastApiWebSocketTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FederationChannelServer:
    """服务端接受一条 channel 所需的全部协作者（全部显式注入，无隐式全局）。"""

    local_gateway_id: str
    gateway_root: Path
    credential_store: FederationCredentialStore
    handler: FederationRequestHandler
    local_role: Literal["hub", "spoke"]
    on_channel_ready: Callable[[FederationChannelSession], None]
    on_channel_closed: Callable[[str], None]


async def serve_federation_channel(
    *,
    websocket: WebSocket,
    server: FederationChannelServer,
) -> None:
    """在 WebSocket 上跑一条 peer RPC channel，直到对端断开或本地失败。"""

    await websocket.accept()
    transport = FastApiWebSocketTransport(websocket)
    try:
        credential = _authenticate(websocket, server.credential_store)
        session = await _perform_server_handshake(
            transport=transport, server=server, credential=credential
        )
    except FederationError as error:
        logger.warning(
            "联邦 channel 握手失败: code=%s, reason=%s", error.code, error.message
        )
        await websocket.close(code=1008, reason=f"{error.code}: {error.message}"[:120])
        return
    server.on_channel_ready(session)
    session.start()
    try:
        reason = await session.join()
        if reason is not None:
            logger.warning(
                "联邦 channel 关闭: channel=%s, code=%s, reason=%s",
                session.channel_instance_id,
                reason.code,
                reason.message,
            )
    finally:
        server.on_channel_closed(session.channel_instance_id)


def _authenticate(
    websocket: WebSocket,
    credential_store: FederationCredentialStore,
) -> FederationCredential:
    token = websocket.headers.get("x-boxteam-federation-token")
    if token is None or not token.strip():
        raise FederationError(
            FEDERATION_MISSING_CREDENTIAL, "channel 握手缺少联邦凭据"
        )
    try:
        return credential_store.verify(token)
    except PermissionError as error:
        raise FederationError(
            FEDERATION_INVALID_CREDENTIAL, f"channel 联邦凭据校验失败: {error}"
        ) from error


async def _perform_server_handshake(
    *,
    transport: FastApiWebSocketTransport,
    server: FederationChannelServer,
    credential: FederationCredential,
) -> FederationChannelSession:
    raw = await transport.receive_text()
    if raw is None:
        raise FederationError(FEDERATION_CHANNEL_CLOSED, "对端在 hello 前断开")
    frame = FederationFrame.decode(raw)
    if frame.frame_type != FRAME_HELLO:
        raise FederationError(
            FEDERATION_MALFORMED_FRAME,
            f"握手首帧必须是 hello，实际为 {frame.frame_type}",
        )
    hello_gateway_id, hello_public_key, channel_epoch = parse_hello_payload(
        frame.payload
    )
    peer = peer_identity_from_hello(
        expected_peer_gateway_id=credential.peer_gateway_id,
        connection_id=credential.connection_id,
        hello_gateway_id=hello_gateway_id,
        hello_public_key_pem=hello_public_key,
    )
    private_key = load_or_create_signing_key(server.gateway_root)
    handshake = ChannelHandshake(
        channel_instance_id=new_channel_instance_id(),
        channel_epoch=channel_epoch,
        local_gateway_id=server.local_gateway_id,
        peer=peer,
        local_role=server.local_role,
    )
    await transport.send_text(
        FederationFrame(
            frame_type=FRAME_WELCOME,
            payload={
                "gateway_id": server.local_gateway_id,
                "public_key_pem": public_key_pem(private_key),
                "channel_epoch": channel_epoch,
            },
        ).encode()
    )
    return FederationChannelSession(
        transport=transport,
        handshake=handshake,
        handler=server.handler,
    )


__all__ = ["FederationChannelServer", "serve_federation_channel"]
