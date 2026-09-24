"""``FederationTransport`` 的两个适配器：FastAPI 服务端与 websockets 拨号端。

收帧在断线时统一返回 ``None``（而不是抛不同异常），让 channel 引擎只有一条
显式失败路径；任何底层异常都翻译为 ``None`` 或直接抛出，绝不静默返回空帧。
"""

from __future__ import annotations

from typing import Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect


class _ClosingWebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

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
            return None

    async def close(self, *, code: int, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        await self._websocket.close(code=code, reason=reason)


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
        except Exception:  # noqa: BLE001 - 断线/协议异常统一为 channel 关闭
            return None
        return message if isinstance(message, str) else None

    async def close(self, *, code: int, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        del code, reason
        await self._connection.close()


__all__ = ["FastApiWebSocketTransport", "WebSocketsClientTransport"]
