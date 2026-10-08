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
    FEDERATION_CHANNEL_CLOSED,
    FEDERATION_CHANNEL_HEARTBEAT_TIMEOUT,
    FEDERATION_DEADLINE_EXCEEDED,
    FederationError,
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


class _BlockingCloseTransport(_MemoryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0
        self.close_cancelled = False

    async def close(self, *, code: int, reason: str) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.release_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        await super().close(code=code, reason=reason)


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
            credential_digest="0" * 64,
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
async def test_join_and_concurrent_close_wait_for_transport_close() -> None:
    transport = _BlockingCloseTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    await transport.incoming.put("not-json")
    await asyncio.wait_for(transport.close_started.wait(), timeout=5)
    receiver = next(
        task for task in session._tasks if task.get_name() == "federation-receive"
    )

    concurrent_close = asyncio.create_task(session.close())
    join = asyncio.create_task(session.join())
    await asyncio.sleep(0)

    assert not concurrent_close.done()
    assert not join.done()
    assert not receiver.cancelled()
    assert not transport.close_cancelled
    assert transport.close_calls == 1
    assert transport.closed is None
    with pytest.raises(FederationError) as caught:
        await session.request(method="federation.status", request={})
    assert caught.value.code == FEDERATION_CHANNEL_CLOSED
    assert transport.sent == []

    concurrent_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await concurrent_close
    assert not transport.close_cancelled
    assert not join.done()

    transport.release_close.set()
    await asyncio.wait_for(join, timeout=5)

    assert transport.closed is not None
    assert transport.closed[0] == 1002
    assert "federation-malformed-frame" in transport.closed[1]
    assert not transport.close_cancelled
    assert transport.close_calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "federation.status", "request": {}},
        {"correlation_id": "", "method": "federation.status", "request": {}},
    ],
    ids=["missing-correlation-id", "empty-correlation-id"],
)
@pytest.mark.asyncio
async def test_request_without_correlation_id_fails_channel(
    payload: dict[str, object],
) -> None:
    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    import json

    await transport.incoming.put(
        json.dumps({"frame_type": "request", "payload": payload})
    )

    reason = await asyncio.wait_for(session.wait_closed(), timeout=5)

    assert reason is not None
    assert reason.code == "federation-malformed-frame"
    assert transport.closed is not None
    assert transport.closed[0] == 1002
    assert transport.sent == []
    await session.join()


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
async def test_request_timeout_raises_structured_deadline_error() -> None:
    """channel 层超时必须收敛为稳定 FederationError，不得冒泡裸 asyncio.TimeoutError。

    对端始终不回包时，``request`` 必须在有效 timeout 后抛出携带
    ``federation-deadline-exceeded`` 的 :class:`FederationError`（与联邦其余拒绝
    路径同一种失败表达），MUST NOT 让裸 ``TimeoutError`` 成为第二条正常路径。
    """

    transport = _MemoryTransport()
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    with pytest.raises(FederationError) as error:
        await session.request(method="federation.status", request={}, timeout=0.05)
    assert error.value.code == FEDERATION_DEADLINE_EXCEEDED
    assert not isinstance(error.value, asyncio.TimeoutError)
    assert error.value.detail["method"] == "federation.status"
    assert error.value.detail["timeout_seconds"] == 0.05
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
