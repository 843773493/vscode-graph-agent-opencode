"""channel 引擎的纯单元测试：心跳超时、断线、畸形帧与 correlation 语义。

用内存传输替身直接驱动 :class:`FederationChannelSession`，避免真实网络的时序
不确定性；真实 loopback 握手/RPC 往返由 ``test_federation_channel`` 覆盖。
"""

from __future__ import annotations

import asyncio

import pytest

from app.gateway.federation.channel import (
    ChannelHandshake,
    FederationChannelSession,
)
from app.gateway.federation.errors import (
    FEDERATION_CHANNEL_HEARTBEAT_TIMEOUT,
)
from app.gateway.federation.identity import FederationPeerIdentity


class _MemoryTransport:
    """内存传输替身：可脚本化收帧、记录发送、可切换为挂起。"""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send_text(self, message: str) -> None:
        self.sent.append(message)

    async def receive_text(self) -> str | None:
        return await self.incoming.get()

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = (code, reason)


class _Handler:
    async def handle(self, *, method, request, session, deadline_at):
        return {"echo": method}


def _peer() -> FederationPeerIdentity:
    return FederationPeerIdentity(
        gateway_id="gateway_peer",
        connection_id="rgw_peer",
        public_key_pem="",
    )


def _session(transport: _MemoryTransport, **kwargs) -> FederationChannelSession:
    return FederationChannelSession(
        transport=transport,
        handshake=ChannelHandshake(
            channel_instance_id="chan_test",
            channel_epoch=1,
            local_gateway_id="gateway_local",
            peer=_peer(),
        ),
        handler=_Handler(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_heartbeat_timeout_closes_channel_explicitly() -> None:
    transport = _MemoryTransport()
    session = _session(
        transport,
        heartbeat_interval_seconds=0.02,
        heartbeat_timeout_seconds=0.05,
    )
    session.start()
    reason = await asyncio.wait_for(session.wait_closed(), timeout=5)
    assert reason is not None
    assert reason.code == FEDERATION_CHANNEL_HEARTBEAT_TIMEOUT
    assert transport.closed is not None
    assert transport.closed[0] == 1011


@pytest.mark.asyncio
async def test_peer_disconnect_fails_pending_request() -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    pending = asyncio.create_task(
        session.request(method="federation.status", request={}, timeout=10)
    )
    await asyncio.sleep(0.02)
    # 对端静默断开：收帧返回 None。
    await transport.incoming.put(None)
    with pytest.raises(Exception) as error:
        await asyncio.wait_for(pending, timeout=5)
    assert "federation-channel-closed" in str(error.value)
    assert session.closed is True
    await session.close()


@pytest.mark.asyncio
async def test_malformed_frame_fails_channel_with_protocol_code() -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    await transport.incoming.put("not-json")
    reason = await asyncio.wait_for(session.wait_closed(), timeout=5)
    assert reason is not None
    assert reason.code == "federation-malformed-frame"
    assert transport.closed is not None
    assert transport.closed[0] == 1002
    await session.close()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    await session.close()
    first = transport.closed
    await session.close()
    assert transport.closed == first
    assert session.closed is True


@pytest.mark.asyncio
async def test_request_rejects_unknown_method() -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    with pytest.raises(Exception) as error:
        await session.request(method="federation.nope", request={}, timeout=1)
    assert "federation-unknown-method" in str(error.value)
    await session.close()


@pytest.mark.asyncio
async def test_request_on_closed_channel_fails() -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    await session.close()
    with pytest.raises(Exception) as error:
        await session.request(method="federation.status", request={}, timeout=1)
    assert "federation-channel-closed" in str(error.value)


@pytest.mark.asyncio
async def test_response_with_unknown_correlation_fails_channel() -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    import json

    await transport.incoming.put(
        json.dumps(
            {
                "frame_type": "response",
                "payload": {"correlation_id": "cor_unknown", "ok": True, "result": {}},
            }
        )
    )
    reason = await asyncio.wait_for(session.wait_closed(), timeout=5)
    assert reason is not None
    assert reason.code == "federation-malformed-frame"
    await session.close()


@pytest.mark.asyncio
async def test_channel_epoch_handshake_is_exposed() -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    assert session.channel_epoch == 1
    assert session.peer_gateway_id == "gateway_peer"
    assert session.local_role == "spoke"
    assert session.closed is False
