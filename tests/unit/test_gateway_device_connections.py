from __future__ import annotations

from pathlib import Path

import pytest
from starlette.requests import Request

from app.gateway import device_connections
from app.gateway.credentials import FederationCredentialStore
from app.gateway.device_connections import (
    CreateDeviceConnectionRequest,
    create_device_connection,
    list_device_access_addresses,
    list_device_connections,
    revoke_device_connection,
)


@pytest.mark.asyncio
async def test_device_connection_can_be_created_listed_and_revoked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))

    created_response = await create_device_connection(
        CreateDeviceConnectionRequest(
            device_name="测试手机",
            gateway_url="http://192.168.1.20:8011",
        ),
        _="local-token",
        request_id="request-create",
    )
    created = created_response.data

    assert created.connection.device_name == "测试手机"
    assert created.connection.status == "authorized"
    assert created.connection_info.manifest_url == (
        "http://192.168.1.20:8011/api/gateway/federation/manifest"
    )
    assert created.connection_info.federation_token

    credential_store = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    )
    assert credential_store.verify(
        created.connection_info.federation_token
    ).peer_gateway_id == "device:测试手机"

    listed = await list_device_connections(
        _="local-token",
        request_id="request-list",
    )
    assert [item.device_name for item in listed.data.items] == ["测试手机"]

    revoked = await revoke_device_connection(
        created.connection.connection_id,
        _="local-token",
        request_id="request-revoke",
    )
    assert revoked.data.items == []
    with pytest.raises(PermissionError, match="无效或已过期"):
        credential_store.verify(created.connection_info.federation_token)


@pytest.mark.asyncio
async def test_device_access_addresses_offer_network_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        device_connections,
        "_preferred_network_addresses",
        lambda: set(),
    )
    monkeypatch.setattr(
        device_connections.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("192.168.1.20", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ],
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("127.0.0.1", 8014),
            "path": "/api/gateway/device-connections/access-addresses",
            "headers": [
                (b"x-forwarded-host", b"127.0.0.1:8011"),
                (b"x-forwarded-proto", b"http"),
            ],
        }
    )

    response = await list_device_access_addresses(
        request,
        _="local-token",
        request_id="request-addresses",
    )

    assert [item.url for item in response.data.items] == [
        "http://127.0.0.1:8011",
        "http://192.168.1.20:8011",
    ]
    assert response.data.items[0].is_loopback is True
    assert response.data.items[1].is_loopback is False
