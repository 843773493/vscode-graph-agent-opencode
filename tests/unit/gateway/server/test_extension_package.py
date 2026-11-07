"""包资源的原生授权和透明路由；目标文件与运行资格由 Backend 负责。"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.resource_uri import extension_package_uri
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import extension_host, extension_package
from app.schemas.gateway.extension_installation import ProfileExtensionInventory
from app.schemas.gateway.extension_web import ExtensionWebHostConnectionDTO
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


@pytest.fixture
def package_proxy(inventory_proxy):
    h, calls, handlers = inventory_proxy
    h.application.include_router(extension_package.router)

    def respond(request: httpx.Request) -> httpx.Response:
        data = ({"content_base64": "b2xk"} if request.url.path.endswith("/read") else
                {"type": 1, "ctime": 0, "mtime": 1, "size": 3, "permissions": 1})
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]},
            json={"request_id": request.headers["X-Request-ID"], "data": data})

    handlers[0] = respond
    payload = {"protocol_version": 1, "gateway_id": GATEWAY, "backend_id": h.request.backend_id,
        "resource_uri": extension_package_uri(gateway_id=GATEWAY, backend_id=h.request.backend_id,
            profile_id="default-profile", extension_id="publisher.probe", relative_segments=("dist", "中文.js")),
        "extension_key": "publisher.probe-1.0.0"}
    return h, calls, handlers, payload


@pytest.mark.parametrize("operation", ["read", "stat"])
@pytest.mark.parametrize("capability", [None, "read", "extension_installation_manage", "extension_host_manage"])
@pytest.mark.parametrize("running", [False, True])
def test_package_permissions_follow_field_role_and_authenticated_client(package_proxy, native_control, operation, capability, running):
    h, calls, _, payload = package_proxy
    if running:
        payload.update(view_id="window-a", generation="frozen-generation")
    h.policy.publish({"rules": [] if capability is None else [{"capability": capability,
        "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})
    response = h.client.post(f"/api/gateway/extension-packages/{operation}", json=payload,
        headers={"X-BoxTeam-Federation-Token": h.federation_token, extension_host.CLIENT_HEADER: "original-client",
                 "X-Request-ID": "package-permission"})
    allowed = capability == "extension_host_manage" or not running and capability == "extension_installation_manage"
    assert response.status_code == (200 if allowed else 403), response.text
    assert len(calls) == int(allowed and running)
    assert len(native_control.calls) == int(allowed and not running)
    if allowed:
        forwarded = json.loads(calls[0].content) if running else native_control.calls[0]
        if running:
            assert calls[0].url.path == f"/api/internal/extension-files/{operation}"
            assert calls[0].headers[extension_host.CLIENT_HEADER] == f"peer:{PEER}:original-client"
            assert calls[0].headers["X-Request-ID"] == response.headers["X-Request-ID"] == "package-permission"
            assert "cookie" not in calls[0].headers
        assert forwarded["profile_id"] == "default-profile"
        assert forwarded["extension_id"] == "publisher.probe"
        assert forwarded["extension_key"] == payload["extension_key"]
        assert forwarded["relative_segments"] == ["dist", "中文.js"]
        assert forwarded["current_profile_member"] is not running
        assert "resource_uri" not in forwarded
        assert response.json()["data"]["resource_uri"] == payload["resource_uri"]
        assert response.json()["data"]["extension_key"] == payload["extension_key"]



@pytest.mark.parametrize("wrong", ["uri-gateway", "uri-backend", "half-binding"])
def test_package_invalid_binding_is_rejected_before_target_read(package_proxy, wrong):
    h, calls, _, payload = package_proxy
    if wrong == "uri-gateway":
        payload["resource_uri"] = payload["resource_uri"].replace(GATEWAY, PEER)
    elif wrong == "uri-backend":
        payload["resource_uri"] = payload["resource_uri"].replace(h.request.backend_id, str(uuid4()))
    else:
        payload["generation"] = "old"
    result = h.client.post("/api/gateway/extension-packages/read", json=payload, headers={"X-Local-Token": h.token})
    assert result.status_code == 422 and calls == []


def test_package_running_revocation_cannot_be_replaced_by_install_permission(package_proxy):
    h, calls, handlers, payload = package_proxy
    payload.update(view_id="window-a", generation="frozen-generation")
    h.policy.publish({"rules": [{"capability": permission, "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}
        for permission in ("extension_host_manage", "extension_installation_manage")]})
    original = handlers[0]

    def revoke(request: httpx.Request) -> httpx.Response:
        h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow",
                                    "principal_ref": principal_ref_for_gateway(PEER)}]})
        return original(request)

    handlers[0] = revoke
    result = h.client.post("/api/gateway/extension-packages/read", json=payload,
        headers={"X-BoxTeam-Federation-Token": h.federation_token, extension_host.CLIENT_HEADER: "original-client"})
    assert result.status_code == 403 and len(calls) == 1
    assert "content_base64" not in result.text


@pytest.mark.parametrize("invalidate", ["route", "user"])
def test_web_control_channel_without_workspace_uses_original_supervision(package_proxy, monkeypatch, invalidate):
    h, _, _, _ = package_proxy

    async def upstream(websocket, *, headers, payload, expected_client_id, **_):
        binding = ExtensionWebHostConnectionDTO(**payload.model_dump(exclude={"gateway_connection_id"}),
            client_id=expected_client_id, generation=str(uuid4()), request_id=headers["X-Request-ID"],
            inventory=ProfileExtensionInventory.model_validate({"profile_id": "default-profile", "extensions": [{"extension_key": "publisher.probe-1.0.0",
                "source": "profile", "description": {}, "is_valid": True, "validations": []}]}))
        await websocket.send_text(binding.model_dump_json())
        await asyncio.Event().wait()

    monkeypatch.setattr(extension_host, "_upstream", upstream)
    payload = {"protocol_version": 1, "gateway_id": GATEWAY, "backend_id": h.request.backend_id,
        "view_id": "unique-window", "workspace_ids": [], "profile_id": "default-profile", "extension_keys": ["publisher.probe-1.0.0"]}
    path = f"/api/gateway/extension-web-hosts/channel?token={h.token}"
    with h.client.websocket_connect(path, headers={"X-Request-ID": "web-control"}) as socket:
        socket.send_json(payload)
        binding = socket.receive_json()
        assert binding["workspace_ids"] == [] and binding["request_id"] == "web-control"
        assert "initialization" not in binding and "gateway_connection_id" not in binding
        assert h.client.portal is not None
        if invalidate == "route":
            h.client.portal.call(h.registry.invalidate_route, (GATEWAY, h.request.workspace_ids[0]))
        else:
            h.client.portal.call(h.users.release, h.access)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_idle_remote_web_control_wakes_on_original_registry_publication(package_proxy, monkeypatch):
    h, _, _, _ = package_proxy
    connection = RemoteGatewayConnection(connection_id="test-peer", name="Remote", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual")
    original_runtime = WorkspaceRuntime(service_urls={"workspace_api": "http://old-remote.test"})
    h.registry.upsert_remote_gateway(connection, runtime=original_runtime)

    async def upstream(websocket, *, expected_client_id, payload, headers, url, **_):
        assert url == "ws://old-remote.test/api/gateway/extension-web-hosts/channel"
        assert "X-Local-Token" not in headers
        assert expected_client_id.startswith(f"peer:{GATEWAY}:")
        await websocket.send_json({"generation": "remote-web"})
        await asyncio.Event().wait()

    monkeypatch.setattr(extension_host, "_upstream", upstream)
    payload = {"protocol_version": 1, "gateway_id": PEER, "backend_id": h.request.backend_id,
        "gateway_connection_id": "test-peer", "view_id": "unique-window", "workspace_ids": [],
        "profile_id": "default-profile", "extension_keys": ["publisher.probe-1.0.0"]}
    with h.client.websocket_connect(f"/api/gateway/extension-web-hosts/channel?token={h.token}") as socket:
        socket.send_json(payload)
        assert socket.receive_json() == {"generation": "remote-web"}
        assert h.client.portal is not None
        assert h.registry._remote_gateway_reference_counts == {"test-peer": 1}
        assert h.registry._commit_observers
        h.client.portal.call(lambda: h.registry.upsert_remote_gateway(connection,
            runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://new-remote.test"})))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert not h.registry._remote_gateway_reference_counts
    assert not h.registry._commit_observers
