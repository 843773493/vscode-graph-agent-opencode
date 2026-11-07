"""市场安装与元数据沿原安装域、授权及原始回执，不依赖工作区或运行宿主。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.runtime.workspace import WorkspaceRuntime
from tests.unit.gateway.server.test_extension_host import GATEWAY, PEER
from tests.unit.gateway.server.test_extension_installation import (
    host_gateway as host_gateway,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_extension_installation import (
    host_initialization as host_initialization,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_extension_installation import (
    inventory_proxy as inventory_proxy,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_extension_installation import (
    native_control as native_control,  # noqa: PLC0414
)

BASE = "/api/gateway/extension-installations/"


@pytest.fixture
def gallery_gateway(inventory_proxy, native_control):
    h, calls, handlers = inventory_proxy
    inventory = {"profile_id": "default-profile", "target_platform": "win32-arm64", "extensions": []}
    installed = {"id": "publisher.probe", "version": "1.0.0", "profile_id": "default-profile", "reused": True}

    async def native(command):
        return {"result": {"installed": installed, "inventory": inventory} if command["method"] == "install-gallery" else inventory}

    def remote(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        data = {key: payload[key] for key in ("protocol_version", "gateway_id", "backend_id")}
        data.update(profile_scope="default", inventory=inventory)
        if request.url.path.endswith("/install-gallery"):
            data["installed"] = installed
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]},
            json={"request_id": request.headers["X-Request-ID"], "data": data})

    native_control.handlers[0] = native
    handlers[0] = remote
    return h, calls, native_control, inventory


def _payload(h, operation):
    payload = {"protocol_version": 1, "gateway_id": GATEWAY, "backend_id": h.request.backend_id,
        "extension_id": "publisher.probe", "version": "1.0.0"}
    if operation == "install-gallery":
        payload["options"] = {"pinned": False, "install_given_version": True}
    else:
        payload.update(profile_id="default-profile", metadata={"pinned": False}, refresh_from_gallery=True)
    return payload


@pytest.mark.parametrize("operation", ["install-gallery", "metadata"])
def test_native_operations_use_installation_domain_without_workspace(gallery_gateway, operation):
    h, calls, native, _ = gallery_gateway
    payload = _payload(h, operation)
    response = h.client.post(BASE + operation, json=payload, headers={"X-Local-Token": h.token, "X-Request-ID": "gallery-native"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["request_id"] == response.headers["X-Request-ID"] == "gallery-native"
    assert result["data"]["backend_id"] == payload["backend_id"]
    assert result["data"]["inventory"]["target_platform"] == "win32-arm64"
    assert calls == [] and len(native.calls) == 1
    command = native.calls[0]
    assert command == {"method": operation, **{key: value for key, value in payload.items()
        if key not in {"protocol_version", "gateway_id", "backend_id"}}}
    assert "workspace_ids" not in result["data"]


@pytest.mark.parametrize("operation", ["install-gallery", "metadata"])
@pytest.mark.parametrize("capability", [None, "extension_host_manage", "extension_installation_manage"])
def test_installation_permission_is_required_for_both_writes(gallery_gateway, operation, capability):
    h, calls, native, _ = gallery_gateway
    h.policy.publish({"rules": [] if capability is None else [{"capability": capability,
        "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})
    response = h.client.post(BASE + operation, json=_payload(h, operation),
        headers={"X-BoxTeam-Federation-Token": h.federation_token})
    allowed = capability == "extension_installation_manage"
    assert response.status_code == (200 if allowed else 403), response.text
    assert len(native.calls) == int(allowed) and not calls


@pytest.mark.parametrize("operation", ["install-gallery", "metadata"])
def test_relay_keeps_explicit_connection_and_does_not_change_local_domain(gallery_gateway, operation):
    h, calls, native, _ = gallery_gateway
    h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id="test-peer", name="B", host="remote.test",
        port=22, username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://chosen-b.test"}))
    payload = {**_payload(h, operation), "gateway_id": PEER, "gateway_connection_id": "test-peer"}
    response = h.client.post(BASE + operation, json=payload, headers={"X-Local-Token": h.token})
    assert response.status_code == 200, response.text
    assert not native.calls and len(calls) == 1
    assert calls[0].url == "http://chosen-b.test" + BASE + operation
    assert json.loads(calls[0].content) == {key: value for key, value in payload.items() if key != "gateway_connection_id"}
    assert not h.registry._remote_gateway_reference_counts


@pytest.mark.parametrize(("operation", "field", "value"), [
    ("install-gallery", "options", {"donot_verify_signature": True}),
    ("install-gallery", "options", {"pinned": "false"}),
    ("metadata", "metadata", {"id": "forged-gallery-uuid"}),
    ("metadata", "refresh_from_gallery", 1),
])
def test_unregistered_or_untyped_options_are_rejected_before_native_owner(gallery_gateway, operation, field, value):
    h, calls, native, _ = gallery_gateway
    response = h.client.post(BASE + operation, json={**_payload(h, operation), field: value}, headers={"X-Local-Token": h.token})
    assert response.status_code == 422 and not calls and not native.calls


def test_metadata_does_not_disclose_receipt_from_different_profile(gallery_gateway):
    h, _, _, inventory = gallery_gateway
    inventory["profile_id"] = "other-profile"
    response = h.client.post(BASE + "metadata", json=_payload(h, "metadata"), headers={"X-Local-Token": h.token})
    assert response.status_code == 409 and "data" not in response.json()
