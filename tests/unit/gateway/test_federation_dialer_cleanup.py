from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.gateway.federation import dialer
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.protocol import FRAME_WELCOME, FederationFrame


class FakeConnection:
    def __init__(
        self,
        *,
        blocked_stage: str | None = None,
        send_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.blocked_stage = blocked_stage
        self.send_error = send_error
        self.close_error = close_error
        self.entered = asyncio.Event()
        self.close_calls = 0
        self.received = FederationFrame(
            frame_type=FRAME_WELCOME,
            payload={
                "gateway_id": "peer",
                "public_key_pem": "peer-public-key",
                "channel_epoch": 1,
            },
        ).encode()

    async def send(self, message: str) -> None:
        del message
        if self.send_error is not None:
            raise self.send_error
        if self.blocked_stage == "send":
            self.entered.set()
            await asyncio.Event().wait()

    async def recv(self) -> str:
        if self.blocked_stage == "recv":
            self.entered.set()
            await asyncio.Event().wait()
        return self.received

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _patch_dialer(
    monkeypatch: pytest.MonkeyPatch,
    connection: FakeConnection,
) -> None:
    async def fake_connect(
        url: str, *, additional_headers: dict[str, str]
    ) -> FakeConnection:
        del url, additional_headers
        return connection

    monkeypatch.setattr(dialer, "connect", fake_connect)
    monkeypatch.setattr(dialer, "load_or_create_signing_key", lambda root: object())
    monkeypatch.setattr(dialer, "public_key_pem", lambda key: "local-public-key")
    monkeypatch.setattr(
        dialer,
        "peer_identity_from_hello",
        lambda **kwargs: FederationPeerIdentity(
            gateway_id="peer",
            connection_id="connection",
            public_key_pem="peer-public-key",
        ),
    )


def _request() -> dialer.HubDialRequest:
    return dialer.HubDialRequest(
        gateway_url="http://peer.invalid",
        credential_token="token",
        expected_peer_gateway_id="peer",
        connection_id="connection",
    )


async def _dial(connection: FakeConnection) -> dialer.FederationChannelSession:
    return await dialer.dial_spoke_channel(
        request=_request(),
        local_gateway_id="local",
        gateway_root=Path("unused"),
        handler=object(),  # type: ignore[arg-type]
        channel_epoch=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["send", "recv"])
async def test_cancellation_during_handshake_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
    blocked_stage: str,
) -> None:
    connection = FakeConnection(blocked_stage=blocked_stage)
    _patch_dialer(monkeypatch, connection)
    task = asyncio.create_task(_dial(connection))

    await asyncio.wait_for(connection.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_close_failure_does_not_replace_handshake_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handshake_error = ValueError("handshake send failure")
    connection = FakeConnection(
        send_error=handshake_error,
        close_error=OSError("connection close failure"),
    )
    _patch_dialer(monkeypatch, connection)

    with pytest.raises(ValueError) as caught:
        await _dial(connection)

    assert caught.value is handshake_error
    assert any("OSError: connection close failure" in note for note in caught.value.__notes__)
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_successful_handoff_does_not_close_connection_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    _patch_dialer(monkeypatch, connection)

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            self.transport = kwargs["transport"]
            self.started = False

        def start(self) -> None:
            self.started = True

    sessions: list[FakeSession] = []

    def create_session(**kwargs: object) -> FakeSession:
        session = FakeSession(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "FederationChannelSession", create_session)

    returned = await _dial(connection)

    assert returned is sessions[0]
    assert sessions[0].started
    assert connection.close_calls == 0
