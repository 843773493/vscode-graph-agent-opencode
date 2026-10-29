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
from app.gateway.federation.protocol import (
    FRAME_REQUEST,
    FederationFrame,
    build_request_frame,
)


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


class _FailingSendTransport(_MemoryTransport):
    def __init__(self, send_error: Exception) -> None:
        super().__init__()
        self.send_error = send_error

    async def send_text(self, message: str) -> None:
        del message
        raise self.send_error


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


class _FailingCloseTransport(_MemoryTransport):
    def __init__(self, close_error: OSError) -> None:
        super().__init__()
        self.close_error = close_error
        self.close_calls = 0

    async def close(self, *, code: int, reason: str) -> None:
        del code, reason
        self.close_calls += 1
        raise self.close_error


class _BlockingFailingCloseTransport(_FailingCloseTransport):
    def __init__(self, close_error: OSError) -> None:
        super().__init__(close_error)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self, *, code: int, reason: str) -> None:
        del code, reason
        self.close_calls += 1
        self.close_started.set()
        await self.release_close.wait()
        raise self.close_error


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


@pytest.mark.parametrize(
    "send_error",
    [OSError("socket send failed"), TypeError("transport message invalid")],
    ids=["socket-error", "local-type-error"],
)
@pytest.mark.asyncio
async def test_heartbeat_send_failure_preserves_original_cause(
    send_error: Exception,
) -> None:
    transport = _FailingSendTransport(send_error)
    session = _session(
        transport,
        heartbeat_interval_seconds=0.01,
        heartbeat_timeout_seconds=30,
    )
    session.start()

    try:
        reason = await asyncio.wait_for(session.wait_closed(), timeout=5)

        assert reason is not None
        assert reason.code == FEDERATION_CHANNEL_CLOSED
        expected = (
            "channel 心跳发送失败: stage=federation-channel-heartbeat, "
            f"error={type(send_error).__name__}"
        )
        if isinstance(send_error, OSError):
            expected += f", errno={send_error.errno}"
        assert reason.message == expected
        assert reason.detail == {"cause_type": type(send_error).__name__}
        assert reason.__cause__ is send_error
    finally:
        await session.close()
        await session.join()


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


@pytest.mark.asyncio
async def test_wait_closed_close_failure_is_reported_and_join_drains_tasks() -> None:
    close_error = OSError("socket close failed")
    transport = _FailingCloseTransport(close_error)
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    await transport.incoming.put("not-json")

    try:
        with pytest.raises(FederationError) as wait_error:
            await asyncio.wait_for(session.wait_closed(), timeout=5)

        reason = session._closed_reason
        assert reason is not None
        assert reason.code == "federation-malformed-frame"
        assert wait_error.value is reason
        assert any("socket close failed" in note for note in reason.__notes__)

        with pytest.raises(FederationError) as join_error:
            await session.join()

        assert join_error.value is reason
        assert any("socket close failed" in note for note in reason.__notes__)
        assert all(task.done() for task in session._tasks)
        assert transport.close_calls == 1
    finally:
        for task in session._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*session._tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_join_cancellation_survives_transport_close_failure_and_drains_tasks() -> None:
    close_error = OSError("socket close failed")
    transport = _BlockingFailingCloseTransport(close_error)
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    close_task = asyncio.create_task(session.close())
    await asyncio.wait_for(transport.close_started.wait(), timeout=5)

    join_task = asyncio.create_task(session.join())
    await asyncio.sleep(0)
    join_task.cancel()
    transport.release_close.set()

    try:
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await asyncio.wait_for(join_task, timeout=5)
        with pytest.raises(FederationError) as close_failure:
            await asyncio.wait_for(close_task, timeout=5)

        assert cancellation.value.__notes__
        assert any("socket close failed" in note for note in cancellation.value.__notes__)
        assert close_failure.value.code == FEDERATION_CHANNEL_CLOSED
        assert any("socket close failed" in note for note in close_failure.value.__notes__)
        assert all(task.done() for task in session._tasks)
        assert transport.close_calls == 1
    finally:
        for task in (close_task, join_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(close_task, join_task, return_exceptions=True)
        for task in session._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*session._tasks, return_exceptions=True)


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


@pytest.fixture(
    params=[
        "/private/channel/通道.sock",
        r"C:\private\channel\通道.sock",
        r"\\server\private\channel\通道.sock",
    ],
    ids=["posix", "drive", "unc"],
)
def native_channel_error(request: pytest.FixtureRequest) -> OSError:
    return PermissionError(13, "原生传输拒绝" + "诊断" * 80, request.param)


@pytest.mark.parametrize("phase", ["receive", "heartbeat"])
@pytest.mark.parametrize("sink_fails", [False, True])
@pytest.mark.asyncio
async def test_transport_failure_facts_fail_pending_and_drain_tasks(
    native_channel_error: OSError,
    phase: str,
    sink_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = _MemoryTransport()
    release = asyncio.Event()
    sent = asyncio.Event()
    original_send = transport.send_text

    async def receive() -> str | None:
        await release.wait()
        raise native_channel_error

    async def send(message: str) -> None:
        if FederationFrame.decode(message).frame_type == FRAME_REQUEST:
            await original_send(message)
            sent.set()
        else:
            await release.wait()
            raise native_channel_error

    if phase == "receive":
        monkeypatch.setattr(transport, "receive_text", receive)
    monkeypatch.setattr(transport, "send_text", send)
    if sink_fails:
        from app.core import failures

        def fail_sink(*args: object, **kwargs: object) -> None:
            raise OSError("诊断 sink 故障 /private/log-sink")

        monkeypatch.setattr(failures.logger, "error", fail_sink)
    session = _session(
        transport, heartbeat_interval_seconds=0.01, heartbeat_timeout_seconds=30
    )
    session.start()
    pending = asyncio.create_task(
        session.request(method="federation.status", request={}, timeout=5)
    )
    try:
        await asyncio.wait_for(sent.wait(), timeout=5)
        release.set()
        with pytest.raises(FederationError) as caught:
            await asyncio.wait_for(pending, timeout=5)
        reason = await asyncio.wait_for(session.wait_closed(), timeout=5)
        assert reason is caught.value
        assert reason is not None
        label = "channel 收帧失败" if phase == "receive" else "channel 心跳发送失败"
        expected = f"{label}: stage=federation-channel-{phase}, error=PermissionError, errno=13"
        assert reason.message == expected + (
            ", diagnostic_sink_error=OSError" if sink_fails else ""
        )
        assert reason.detail == {"cause_type": "PermissionError"}
        assert reason.__cause__ is native_channel_error
        assert native_channel_error.__traceback__ is not None
        assert reason.code == FEDERATION_CHANNEL_CLOSED
        assert transport.closed is not None
        assert transport.closed == (1011, FEDERATION_CHANNEL_CLOSED)
        assert len(transport.closed[1].encode()) <= 123
        assert session._pending == {}
        if sink_fails:
            assert any(
                "/private/log-sink" in note for note in native_channel_error.__notes__
            )
        else:
            assert any(
                record.exc_info and record.exc_info[1] is native_channel_error
                for record in caplog.records
            )
            assert str(native_channel_error) in caplog.text
    finally:
        release.set()
        await session.close()
        await session.join()
        await asyncio.gather(pending, return_exceptions=True)
    assert all(task.done() for task in session._tasks)


@pytest.mark.asyncio
async def test_handler_failure_sends_facts_then_serves_next_request(
    native_channel_error: OSError,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handle(
        self: _Handler,
        *,
        method: str,
        request: dict[str, object],
        session: FederationChannelSession,
        deadline_at: float | None,
    ) -> dict[str, object]:
        if request.get("fail"):
            raise native_channel_error
        return {"echo": method}

    transport = _MemoryTransport()
    outgoing: asyncio.Queue[str] = asyncio.Queue()
    monkeypatch.setattr(_Handler, "handle", handle)
    monkeypatch.setattr(transport, "send_text", outgoing.put)
    session = _session(transport, heartbeat_interval_seconds=30)
    session.start()
    try:
        requests: list[tuple[str, dict[str, object]]] = [
            ("cor_failure", {"fail": True}),
            ("cor_recovered", {}),
        ]
        for correlation_id, request in requests:
            await transport.incoming.put(
                build_request_frame(
                    correlation_id=correlation_id,
                    method="federation.status",
                    request=request,
                ).encode()
            )
            response = FederationFrame.decode(
                await asyncio.wait_for(outgoing.get(), timeout=5)
            )
            assert response.frame_type == "response"
            if request:
                assert response.payload == {
                    "correlation_id": correlation_id,
                    "ok": False,
                    "error": {
                        "code": "federation-malformed-frame",
                        "message": "联邦方法处理失败: stage=federation-method, error=PermissionError, errno=13",
                        "detail": {},
                    },
                }
            else:
                assert response.payload == {
                    "correlation_id": correlation_id,
                    "ok": True,
                    "result": {"echo": "federation.status"},
                }
            assert session.closed is False
        assert any(
            record.exc_info and record.exc_info[1] is native_channel_error
            for record in caplog.records
        )
        assert str(native_channel_error) in caplog.text
    finally:
        await session.close()
        await session.join()
    assert all(task.done() for task in session._tasks)


@pytest.mark.asyncio
async def test_handler_preserves_registered_error_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = FederationError(
        FEDERATION_DEADLINE_EXCEEDED, "操作期限已耗尽", detail={"deadline_at": 123.0}
    )
    cancellation = asyncio.CancelledError("取消当前联邦请求")
    transport = _MemoryTransport()
    session = _session(transport)
    frame = build_request_frame(
        correlation_id="cor_domain", method="federation.status", request={}
    )
    failure: BaseException = error

    async def handle(self: _Handler, **kwargs: object) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(_Handler, "handle", handle)
    await session._serve_request(frame, "cor_domain")
    assert FederationFrame.decode(transport.sent[0]).payload == {
        "correlation_id": "cor_domain",
        "ok": False,
        "error": error.to_wire(),
    }
    failure = cancellation
    with pytest.raises(asyncio.CancelledError) as caught:
        await session._serve_request(frame, "cor_domain")
    assert caught.value is cancellation
    assert len(transport.sent) == 1
    assert session.closed is False


@pytest.mark.asyncio
async def test_close_transmits_code_and_keeps_complete_local_business_error() -> None:
    transport = _MemoryTransport()
    session = _session(transport)
    error = FederationError(
        FEDERATION_DEADLINE_EXCEEDED,
        "有意义的完整业务说明" * 100,
        detail={"deadline_at": 123.0},
    )
    await session.close(error)
    assert await session.wait_closed() is error
    assert transport.closed is not None
    assert transport.closed == (1011, FEDERATION_DEADLINE_EXCEEDED)
    assert len(transport.closed[1].encode()) <= 123
    await session.join()
