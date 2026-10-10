"""``FederationTransport`` 的两个适配器：FastAPI 服务端与 websockets 拨号端。

只有明确的 WebSocket 关闭才将收帧映射为 ``None``；其它底层异常保持原样抛出，
避免把传输故障伪装为正常关闭。
"""

from __future__ import annotations

from typing import Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.gateway.federation.errors import FEDERATION_MALFORMED_FRAME, FederationError


class _ClosingWebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class FastApiWebSocketTransport:
    """把 FastAPI ``WebSocket`` 适配为 channel 传输。"""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._closed = False

    async def send_text(self, message: str) -> None:
        await self._websocket.send_text(message)

    async def receive_text(self) -> str | None:
        try:
            return await self._websocket.receive_text()
        except WebSocketDisconnect:
            self._closed = True
            return None

    async def close(self, *, code: int, reason: str) -> None:
        if self._closed:
            return
        await self._websocket.close(code=code, reason=reason)
        self._closed = True


class WebSocketsClientTransport:
    """把 ``websockets`` 客户端连接适配为 channel 传输。"""

    def __init__(self, connection: _ClosingWebSocket) -> None:
        self._connection = connection
        self._closed = False

    async def send_text(self, message: str) -> None:
        await self._connection.send(message)

    async def receive_text(self) -> str | None:
        try:
            message = await self._connection.recv()
        except ConnectionClosed:
            return None
        if isinstance(message, str):
            return message
        if isinstance(message, bytes):
            raise FederationError(
                FEDERATION_MALFORMED_FRAME,
                "联邦 channel 收到非文本 WebSocket 帧",
            )
        raise TypeError(
            "WebSocketsClientTransport.recv() 必须返回 str 或 bytes，"
            f"实际为 {type(message).__name__}"
        )

    async def close(self, *, code: int, reason: str) -> None:
        if self._closed:
            return
        del code, reason
        await self._connection.close()
        self._closed = True


__all__ = ["FastApiWebSocketTransport", "WebSocketsClientTransport"]
