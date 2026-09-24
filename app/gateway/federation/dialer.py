"""hub 侧主动拨号：经 SSH ``-L`` loopback 连接 spoke 的 peer RPC channel。

hub 用既有 federation token 认证自己，发送 hello 并读取 welcome 完成对称握手，
随后把连接交给同一个 :class:`FederationChannelSession` 引擎；channel 建立后
双方都能发起 request，因此 spoke 可反向 ``B → A → C``。

断线只刷新临时 route：已提交的 outbox 继续按稳定 ``GlobalThreadAddress`` 重试，
不重新用裸 ID 选路；重连成功会推进 channel epoch，旧 channel 显式让位。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from websockets.asyncio.client import connect

from app.gateway.federation.channel import (
    ChannelHandshake,
    FederationChannelSession,
    FederationRequestHandler,
    new_channel_instance_id,
    parse_hello_payload,
)
from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_MALFORMED_FRAME,
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
from app.gateway.federation.transport import WebSocketsClientTransport

logger = logging.getLogger(__name__)


def channel_websocket_url(gateway_url: str) -> str:
    """把远端 Gateway 的 HTTP base 转为 channel WebSocket URL。"""

    base = gateway_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/api/gateway/federation/channel"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/api/gateway/federation/channel"
    raise ValueError(f"远程 Gateway URL 必须使用 http/https: {gateway_url}")


@dataclass(frozen=True, slots=True)
class HubDialRequest:
    """hub 拨号一次所需参数；``connection_id`` 只作本地配置身份。"""

    gateway_url: str
    credential_token: str
    expected_peer_gateway_id: str
    connection_id: str


async def dial_spoke_channel(
    *,
    request: HubDialRequest,
    local_gateway_id: str,
    gateway_root,
    handler: FederationRequestHandler,
    channel_epoch: int,
) -> FederationChannelSession:
    """拨号并完成握手，返回已启动的 hub 侧 channel session。"""

    if channel_epoch < 1:
        raise FederationError(FEDERATION_CHANNEL_CLOSED, "channel epoch 必须为正")
    url = channel_websocket_url(request.gateway_url)
    private_key = load_or_create_signing_key(gateway_root)
    headers = {"X-BoxTeam-Federation-Token": request.credential_token}
    connection = await connect(url, additional_headers=headers)
    try:
        await connection.send(
            FederationFrame(
                frame_type=FRAME_HELLO,
                payload={
                    "gateway_id": local_gateway_id,
                    "public_key_pem": public_key_pem(private_key),
                    "channel_epoch": channel_epoch,
                },
            ).encode()
        )
        raw = await connection.recv()
        if not isinstance(raw, str):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "welcome 帧必须是文本")
        welcome = FederationFrame.decode(raw)
        if welcome.frame_type != FRAME_WELCOME:
            raise FederationError(
                FEDERATION_MALFORMED_FRAME,
                f"握手第二帧必须是 welcome，实际为 {welcome.frame_type}",
            )
        peer_gateway_id, peer_public_key, peer_epoch = parse_hello_payload(
            welcome.payload
        )
        peer = peer_identity_from_hello(
            expected_peer_gateway_id=request.expected_peer_gateway_id,
            connection_id=request.connection_id,
            hello_gateway_id=peer_gateway_id,
            hello_public_key_pem=peer_public_key,
        )
        session = FederationChannelSession(
            transport=WebSocketsClientTransport(connection),
            handshake=ChannelHandshake(
                channel_instance_id=new_channel_instance_id(),
                channel_epoch=peer_epoch,
                local_gateway_id=local_gateway_id,
                peer=peer,
                local_role="hub",
            ),
            handler=handler,
        )
    except Exception:
        await connection.close()
        raise
    session.start()
    return session


__all__ = ["HubDialRequest", "channel_websocket_url", "dial_spoke_channel"]
