"""联邦错误码闭集与 response wire 边界。"""

from __future__ import annotations

import asyncio

import pytest

from app.gateway.federation import errors
from app.gateway.federation.channel import (
    ChannelHandshake,
    FederationChannelSession,
)
from app.gateway.federation.errors import (
    CLOSE_CODE_INTERNAL_ERROR,
    CLOSE_CODE_POLICY_VIOLATION,
    CLOSE_CODE_PROTOCOL_ERROR,
    FEDERATION_GRANT_EXPIRED,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FederationError,
    close_code_for,
)
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.protocol import FRAME_RESPONSE, FederationFrame


class _Transport:
    async def send_text(self, message: str) -> None:
        del message

    async def receive_text(self) -> str | None:
        return None

    async def close(self, *, code: int, reason: str) -> None:
        del code, reason


class _Handler:
    async def handle(self, *, method, request, session, deadline_at):
        del method, request, session, deadline_at
        return {}


def _session() -> FederationChannelSession:
    return FederationChannelSession(
        transport=_Transport(),
        handshake=ChannelHandshake(
            channel_instance_id="chan_registry_test",
            channel_epoch=1,
            local_gateway_id="gateway_local",
            peer=FederationPeerIdentity(
                gateway_id="gateway_peer",
                connection_id="rgw_peer",
                public_key_pem="",
            ),
            credential_digest="0" * 64,
        ),
        handler=_Handler(),
    )


def test_every_registered_federation_constant_is_constructible_and_serialized() -> None:
    constants = {
        value
        for name, value in vars(errors).items()
        if name.startswith("FEDERATION_") and isinstance(value, str)
    }
    assert constants == errors.FEDERATION_ERROR_CODES

    for code in errors.FEDERATION_ERROR_CODES:
        error = FederationError(code, "registered")
        assert error.to_wire() == {"code": code, "message": "registered", "detail": {}}
        assert close_code_for(error) in {
            CLOSE_CODE_PROTOCOL_ERROR,
            CLOSE_CODE_POLICY_VIOLATION,
            CLOSE_CODE_INTERNAL_ERROR,
        }


@pytest.mark.parametrize(
    "code",
    [
        "made-up-code",
        "unsupported_view",
        "snapshot_changed",
        "read-snapshot-invalid",
        CLOSE_CODE_PROTOCOL_ERROR,
        [],
    ],
)
def test_constructor_rejects_unknown_and_other_domain_codes(code: object) -> None:
    with pytest.raises(ValueError, match="未登记"):
        FederationError(code, "rejected")  # type: ignore[arg-type]


def test_existing_close_code_groups_remain_stable() -> None:
    assert close_code_for(FederationError(FEDERATION_MALFORMED_FRAME, "bad")) == (
        CLOSE_CODE_PROTOCOL_ERROR
    )
    assert close_code_for(FederationError(FEDERATION_GRANT_EXPIRED, "expired")) == (
        CLOSE_CODE_POLICY_VIOLATION
    )
    assert close_code_for(FederationError(FEDERATION_TARGET_NOT_RESOLVABLE, "missing")) == (
        CLOSE_CODE_INTERNAL_ERROR
    )


@pytest.mark.asyncio
async def test_registered_remote_wire_error_keeps_its_contract() -> None:
    session = _session()
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    session._pending["cor_registered"] = future

    session._dispatch_response(
        FederationFrame(
            frame_type=FRAME_RESPONSE,
            payload={
                "correlation_id": "cor_registered",
                "ok": False,
                "error": {
                    "code": FEDERATION_GRANT_EXPIRED,
                    "message": "grant expired",
                    "detail": {"kind": "read"},
                },
            },
        )
    )

    with pytest.raises(FederationError) as error:
        await future
    assert error.value.to_wire() == {
        "code": FEDERATION_GRANT_EXPIRED,
        "message": "grant expired",
        "detail": {"kind": "read"},
    }


@pytest.mark.asyncio
async def test_unknown_remote_wire_error_becomes_protocol_error_with_cause() -> None:
    session = _session()
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    session._pending["cor_unknown"] = future

    with pytest.raises(FederationError) as error:
        session._dispatch_response(
            FederationFrame(
                frame_type=FRAME_RESPONSE,
                payload={
                    "correlation_id": "cor_unknown",
                    "ok": False,
                    "error": {
                        "code": "read-snapshot-invalid",
                        "message": "owner rejection",
                        "detail": {"locator": "/private/path"},
                    },
                },
            )
        )

    assert error.value.code == FEDERATION_MALFORMED_FRAME
    assert error.value.detail == {"remote_code": "read-snapshot-invalid"}
    assert isinstance(error.value.__cause__, ValueError)
    assert close_code_for(error.value) == CLOSE_CODE_PROTOCOL_ERROR
    future.cancel()
