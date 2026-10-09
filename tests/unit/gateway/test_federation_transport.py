from __future__ import annotations

import asyncio

import pytest
from websockets.exceptions import ConnectionClosed

from app.gateway.federation.channel import ChannelHandshake, FederationChannelSession
from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_MALFORMED_FRAME,
    FederationError,
)
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.transport import (
    FastApiWebSocketTransport,
    WebSocketsClientTransport,
)


class FakeConnection:
    def __init__(self, result: str | bytes | Exception) -> None:
        self.result = result

    async def send(self, message: str) -> None:
        del message

    async def recv(self) -> str | bytes:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        return None


class _FailOnceConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__("frame")
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise OSError("connection close failed")


class _FailOnceWebSocket:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self, *, code: int, reason: str) -> None:
        del code, reason
        self.close_calls += 1
        if self.close_calls == 1:
            raise OSError("websocket close failed")


async def _receive(result: str | bytes | Exception) -> str | None:
    return await WebSocketsClientTransport(FakeConnection(result)).receive_text()


@pytest.mark.asyncio
async def test_text_is_returned_and_connection_close_is_none() -> None:
    assert await _receive("frame") == "frame"
    assert await _receive(ConnectionClosed(None, None)) is None


@pytest.mark.asyncio
async def test_binary_frame_is_an_explicit_protocol_error() -> None:
    with pytest.raises(FederationError) as caught:
        await _receive(b"frame")
    assert caught.value.code == FEDERATION_MALFORMED_FRAME


@pytest.mark.asyncio
async def test_programming_error_from_recv_is_preserved() -> None:
    with pytest.raises(TypeError, match="recv failure"):
        await _receive(TypeError("recv failure"))


@pytest.mark.asyncio
async def test_client_adapter_close_failure_does_not_mark_transport_closed() -> None:
    connection = _FailOnceConnection()
    transport = WebSocketsClientTransport(connection)

    with pytest.raises(OSError, match="connection close failed"):
        await transport.close(code=1000, reason="shutdown")
    await transport.close(code=1000, reason="shutdown")

    assert connection.close_calls == 2


@pytest.mark.asyncio
async def test_server_adapter_close_failure_does_not_mark_transport_closed() -> None:
    websocket = _FailOnceWebSocket()
    transport = FastApiWebSocketTransport(websocket)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="websocket close failed"):
        await transport.close(code=1000, reason="shutdown")
    await transport.close(code=1000, reason="shutdown")

    assert websocket.close_calls == 2


def _channel(transport: WebSocketsClientTransport) -> FederationChannelSession:
    class Handler:
        async def handle(self, *, method, request, session, deadline_at):
            del method, request, session, deadline_at
            return {}

    return FederationChannelSession(
        transport=transport,
        handshake=ChannelHandshake(
            channel_instance_id="chan_test",
            channel_epoch=1,
            local_gateway_id="gateway_local",
            peer=FederationPeerIdentity(
                gateway_id="gateway_peer",
                connection_id="connection_peer",
                public_key_pem="",
            ),
            credential_digest="0" * 64,
        ),
        handler=Handler(),
    )


@pytest.mark.asyncio
async def test_receive_loop_preserves_transport_federation_error_for_pending_request() -> None:
    session = _channel(WebSocketsClientTransport(FakeConnection(b"binary frame")))
    pending: asyncio.Future[dict[str, object]] = (
        asyncio.get_running_loop().create_future()
    )
    session._pending["request-1"] = pending

    await session._receive_loop()

    reason = session._closed_reason
    assert reason is not None
    assert reason.code == FEDERATION_MALFORMED_FRAME
    with pytest.raises(FederationError) as caught:
        await pending
    assert caught.value.code == FEDERATION_MALFORMED_FRAME


@pytest.mark.asyncio
async def test_receive_loop_preserves_programming_error_cause_for_pending_request() -> None:
    original = TypeError("recv failure")
    session = _channel(WebSocketsClientTransport(FakeConnection(original)))
    pending: asyncio.Future[dict[str, object]] = (
        asyncio.get_running_loop().create_future()
    )
    session._pending["request-1"] = pending

    await session._receive_loop()

    reason = session._closed_reason
    assert reason is not None
    assert reason.code == FEDERATION_CHANNEL_CLOSED
    assert reason.__cause__ is original
    assert original.__traceback__ is not None
    with pytest.raises(FederationError) as caught:
        await pending
    assert caught.value is reason
