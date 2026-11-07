"""目标配置沿原安装域路由；空工作区视图仍保持认证与撤权边界。"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import extension_configuration, extension_host
from app.services.infrastructure.config.source_uri import extension_configuration_uri
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
def configuration_proxy(inventory_proxy):
    h, calls, handlers = inventory_proxy
    h.application.include_router(extension_configuration.router)

    def reply(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["backend_id"] != h.request.backend_id:
            return httpx.Response(409, headers={"X-Request-ID": request.headers["X-Request-ID"]},
                json={"request_id": request.headers["X-Request-ID"], "detail": "配置不属于原 Backend 代际"})
        data = {"resource_uri": payload["resource_uri"]}
        if request.url.path.endswith("/read"):
            data["content_base64"] = "dGFyZ2V0"
        else:
            data.update(type=1, ctime=1, mtime=2, size=6, permissions=1)
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]},
            json={"request_id": request.headers["X-Request-ID"], "data": data})

    handlers[0] = reply
    payload = {"protocol_version": 1, "gateway_id": GATEWAY, "backend_id": h.request.backend_id,
        "view_id": "empty-view", "workspace_ids": [], "generation": "web-generation",
        "resource_uri": extension_configuration_uri(gateway_id=GATEWAY, backend_id=h.request.backend_id,
            profile_id="default-profile")}
    return h, calls, handlers, payload


@pytest.mark.parametrize("root_count", [0, 1, 2])
def test_configuration_routes_exact_backend_with_empty_or_complete_view(configuration_proxy, root_count):
    h, calls, _, payload = configuration_proxy
    payload["workspace_ids"] = h.request.workspace_ids[:root_count]
    response = h.client.post("/api/gateway/extension-configurations/read", json=payload,
        headers={"X-Local-Token": h.token, "X-Request-ID": "target-settings"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["content_base64"] == "dGFyZ2V0"
    assert calls[0].url.path == "/api/internal/extension-configurations/read"
    assert json.loads(calls[0].content) == payload
    assert calls[0].headers[extension_host.CLIENT_HEADER]
    assert calls[0].headers["X-Request-ID"] == response.headers["X-Request-ID"] == "target-settings"
    rejected = h.client.post("/api/gateway/extension-configurations/read", json={**payload, "backend_id": str(uuid4())},
        headers={"X-Local-Token": h.token})
    assert rejected.status_code == 409 and len(calls) == (2 if root_count else 1)


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("capabilities", [[], ["configuration_read"], ["extension_host_manage"],
    ["extension_installation_manage", "configuration_read", "configuration_write"],
    ["extension_host_manage", "configuration_read"],
    ["extension_host_manage", "configuration_read", "configuration_write"]])
def test_empty_view_configuration_retains_distinct_native_permissions(configuration_proxy, operation, capabilities):
    h, calls, _, payload = configuration_proxy
    h.policy.publish({"rules": [{"capability": capability, "effect": "allow",
        "principal_ref": principal_ref_for_gateway(PEER)} for capability in capabilities]})
    if operation == "write":
        payload.update(content_base64="dGFyZ2V0", options={"create": True, "overwrite": True,
            "unlock": False, "atomic": False, "append": False})
    response = h.client.post(f"/api/gateway/extension-configurations/{operation}", json=payload,
        headers={"X-BoxTeam-Federation-Token": h.federation_token, extension_host.CLIENT_HEADER: "original-client"})
    required = {"extension_host_manage", "configuration_read"} | ({"configuration_write"} if operation == "write" else set())
    allowed = required.issubset(capabilities)
    assert response.status_code == (200 if allowed else 403), response.text
    assert len(calls) == int(allowed)
    if allowed:
        assert calls[0].headers[extension_host.CLIENT_HEADER] == f"peer:{PEER}:original-client"


@pytest.mark.parametrize("replace_route", [False, True])
def test_configuration_relay_preserves_connection_and_rejects_late_old_route(configuration_proxy, replace_route):
    h, calls, handlers, payload = configuration_proxy
    for connection_id in ("test-peer", "other-route"):
        h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id=connection_id, name="same-name",
            host="remote.test", port=22, username="unit", private_key_path=None, ssh_config_host=None,
            remote_gateway_port=8014, remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": f"http://{connection_id}.test"}))
    payload.update(gateway_id=PEER, gateway_connection_id="test-peer", resource_uri=extension_configuration_uri(
        gateway_id=PEER, backend_id=h.request.backend_id, profile_id="default-profile"))
    original = handlers[0]

    def reply(request: httpx.Request) -> httpx.Response:
        if replace_route:
            connection = h.registry.remote_gateway_connections()[0]
            assert connection.connection_id == "test-peer"
            h.registry.upsert_remote_gateway(connection, runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://new.test"}))
        return original(request)

    handlers[0] = reply
    response = h.client.post("/api/gateway/extension-configurations/read", json=payload,
        headers={"X-Local-Token": h.token, "X-Request-ID": "remote-settings"})
    assert response.status_code == (409 if replace_route else 200), response.text
    assert calls[0].url == "http://test-peer.test/api/gateway/extension-configurations/read"
    assert "gateway_connection_id" not in json.loads(calls[0].content)
    assert calls[0].headers["X-Request-ID"] == response.headers["X-Request-ID"] == "remote-settings"
    assert not h.registry._remote_gateway_reference_counts
    if replace_route:
        assert "content_base64" not in response.text
