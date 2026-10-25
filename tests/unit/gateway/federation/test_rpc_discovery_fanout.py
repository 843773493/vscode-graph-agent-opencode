from __future__ import annotations

import asyncio
from typing import cast

import pytest

from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import (
    FederationCatalogPort,
    FederationSessionMainPort,
    FederationSpoke,
)
from app.gateway.federation.protocol import METHOD_DISCOVERY
from app.gateway.federation.rpc import FederationRpcService
from app.gateway.federation.store import FederationControlStore


class _SpokeDirectory:
    def __init__(self, spokes: tuple[FederationSpoke, ...]) -> None:
        self.spokes = spokes

    def active_spokes(self) -> tuple[FederationSpoke, ...]:
        return self.spokes


class _EncodedGrant:
    def encode(self) -> str:
        return "test-grant"


class _Channel:
    def __init__(self, gateway_id: str) -> None:
        self.gateway_id = gateway_id


@pytest.fixture(autouse=True)
def _request_scope():
    token = set_current_request_id("req_federation_fanout_test")
    yield
    reset_current_request_id(token)


def _service(
    monkeypatch: pytest.MonkeyPatch,
    spokes: tuple[FederationSpoke, ...],
    channels: dict[str, _Channel],
) -> FederationRpcService:
    service = FederationRpcService(
        gateway_id="gateway_hub",
        policy_store=cast(FederationPolicyStore, object()),
        control_store=cast(FederationControlStore, object()),
        signing_key=object(),
        catalog=cast(FederationCatalogPort, object()),
        session_main=cast(FederationSessionMainPort, object()),
        credential_store=cast(FederationCredentialStore, object()),
        spoke_directory=_SpokeDirectory(spokes),
    )

    def authorize(
        self: FederationRpcService,
        **_arguments: object,
    ) -> None:
        return None

    def channel_to(
        self: FederationRpcService,
        gateway_id: str,
        *,
        qualified: bool = False,
    ) -> _Channel:
        return channels[gateway_id]

    def check_channel(self: FederationRpcService, _session: object) -> None:
        return None

    def sign_grant(self: FederationRpcService, **_arguments: object) -> _EncodedGrant:
        return _EncodedGrant()

    monkeypatch.setattr(FederationRpcService, "_authorize", authorize)
    monkeypatch.setattr(FederationRpcService, "channel_to", channel_to)
    monkeypatch.setattr(FederationRpcService, "_check_channel", check_channel)
    monkeypatch.setattr(FederationRpcService, "_sign_grant", sign_grant)
    return service


@pytest.mark.asyncio
async def test_discovery_fanout_is_concurrent_and_keeps_directory_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spokes = tuple(
        FederationSpoke(gateway_id=f"gateway_{index}", connection_id=f"connection_{index}")
        for index in range(3)
    )
    started: set[str] = set()
    all_started = asyncio.Event()
    active = 0
    peak_active = 0

    class ConcurrentChannel(_Channel):
        async def request(
            self,
            *,
            method: str,
            request: dict[str, object],
            timeout: float,
            deadline_at: float | None,
        ) -> dict[str, object]:
            nonlocal active, peak_active
            assert method == METHOD_DISCOVERY
            active += 1
            peak_active = max(peak_active, active)
            started.add(self.gateway_id)
            if len(started) == len(spokes):
                all_started.set()
            try:
                await asyncio.wait_for(all_started.wait(), timeout=1)
                await asyncio.sleep((len(spokes) - int(self.gateway_id[-1])) * 0.01)
                return {"matches": [{"gateway_id": self.gateway_id}]}
            finally:
                active -= 1

    channels = {
        spoke.gateway_id: ConcurrentChannel(spoke.gateway_id) for spoke in spokes
    }
    service = _service(monkeypatch, spokes, channels)

    matches = await service._fan_out_discovery(
        session_id="ses_test",
        workspace_id=None,
        gateway_id=None,
        origin="gateway_hub",
        origin_channel=None,
        path=("gateway_hub",),
        deadline_at=None,
    )

    assert peak_active == len(spokes)
    assert [match["gateway_id"] for match in matches] == [
        spoke.gateway_id for spoke in spokes
    ]


@pytest.mark.asyncio
async def test_discovery_fanout_cancels_pending_requests_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spokes = tuple(
        FederationSpoke(gateway_id=f"gateway_{index}", connection_id=f"connection_{index}")
        for index in range(3)
    )
    started: set[str] = set()
    cancelled: set[str] = set()
    all_started = asyncio.Event()
    expected_error = RuntimeError("spoke request failed")

    class FailingChannel(_Channel):
        async def request(
            self,
            *,
            method: str,
            request: dict[str, object],
            timeout: float,
            deadline_at: float | None,
        ) -> dict[str, object]:
            started.add(self.gateway_id)
            if len(started) == len(spokes):
                all_started.set()
            try:
                await all_started.wait()
                if self.gateway_id == "gateway_0":
                    raise expected_error
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.add(self.gateway_id)
                raise

    channels = {
        spoke.gateway_id: FailingChannel(spoke.gateway_id) for spoke in spokes
    }
    service = _service(monkeypatch, spokes, channels)

    with pytest.raises(RuntimeError) as failure:
        await service._fan_out_discovery(
            session_id="ses_test",
            workspace_id=None,
            gateway_id=None,
            origin="gateway_hub",
            origin_channel=None,
            path=("gateway_hub",),
            deadline_at=None,
        )

    assert failure.value is expected_error
    assert started == {spoke.gateway_id for spoke in spokes}
    assert cancelled == {"gateway_1", "gateway_2"}
