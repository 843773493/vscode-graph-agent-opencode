"""库存判权与路由使用原 Gateway；仅下游 HTTP 回执为受控边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.registry import WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import extension_host
from tests.unit.gateway.server.test_extension_host import (
    GATEWAY,
    PEER,
    Harness,
)
from tests.unit.gateway.server.test_extension_host import (
    host_gateway as host_gateway,  # noqa: PLC0414
)
from tests.unit.gateway.server.test_extension_host import (
    host_initialization as host_initialization,  # noqa: PLC0414
)

PATH = "/api/gateway/extension-installations/inventory"
ResponseHandler = Callable[[httpx.Request], httpx.Response]


def _data(request: httpx.Request) -> dict[str, object]:
    payload = json.loads(request.content)
    source = payload.pop("source_uri", None)
    result = {**payload, "profile_scope": "default", "inventory": {
        "profile_id": "default-profile", "extensions": [{"source": "profile", "description": {
            "identifier": {"value": "publisher.probe"}, "version": "1.0.0",
            "extensionLocation": {"scheme": "file", "path": "/target/extensions/probe"},
            "contributes": {"commands": [{"command": "probe.run", "title": "原始贡献"}]},
        }, "is_valid": False, "validations": [{"severity": 3, "message": "原 manifest 缺失"}]}],
    }}
    if source is not None:
        result["installed"] = {"id": "publisher.probe", "version": "1.0.0", "profile_id": "default-profile", "reused": False}
    return result


def _reply(request: httpx.Request, data: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
        "request_id": request.headers["X-Request-ID"], "data": _data(request) if data is None else data,
    })


@pytest.fixture
def inventory_proxy(host_gateway: Harness) -> Iterator[tuple[Harness, list[httpx.Request], list[ResponseHandler]]]:
    h = host_gateway
    requests: list[httpx.Request] = []
    handlers: list[ResponseHandler] = [_reply]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handlers[0](request)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    h.application.state.http_client = upstream
    try:
        yield h, requests, handlers
    finally:
        assert h.client.portal is not None
        h.client.portal.call(upstream.aclose)
        assert all(h.registry.route_reference_counts(target.key) == (0, 0) for target in h.registry.targets())


def _payload(h: Harness, operation: str = "inventory") -> dict[str, object]:
    result = h.request.model_dump(exclude={"view_id"})
    if operation == "install":
        result["source_uri"] = f"boxteam://{GATEWAY}/workspace/{h.request.workspace_ids[0]}/resources/files/root/probe.vsix"
    return result


def test_inventory_forwards_only_explicit_owner_and_control_headers(inventory_proxy) -> None:
    h, requests, _ = inventory_proxy
    other = WorkspaceTarget(gateway_id=GATEWAY, workspace_id=str(uuid4()), name="same", root_path="/other",
        backend_url="http://127.0.0.1:41101", connection_kind="local")
    h.registry.upsert(other, activate=True)
    response = h.client.post(PATH, json=_payload(h), headers={"X-Local-Token": h.token,
        "X-Request-ID": "inventory-target", extension_host.CLIENT_HEADER: "forged"})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "inventory-target"
    [forwarded] = requests
    assert forwarded.url.path == "/api/internal/extension-installations/inventory" and forwarded.url.port == 41100
    assert json.loads(forwarded.content) == _payload(h)
    assert forwarded.headers["X-BoxTeam-Gateway-Id"] == GATEWAY and forwarded.headers["X-Local-Token"] == h.token
    assert "cookie" not in forwarded.headers and extension_host.CLIENT_HEADER not in forwarded.headers
    assert response.json()["data"] == _data(forwarded)
    assert response.json()["data"]["inventory"]["extensions"][0]["is_valid"] is False
    assert h.registry.active_workspace == other.key and h.calls == []


def test_inventory_requires_token_and_user_lease_before_transport(inventory_proxy) -> None:
    h, requests, _ = inventory_proxy
    assert h.client.post(PATH, json=_payload(h)).status_code == 401
    h.client.cookies.clear()
    assert h.client.post(PATH, json=_payload(h), headers={"X-Local-Token": h.token}).status_code == 401
    assert requests == []


@pytest.mark.parametrize("operation", ["inventory", "install"])
def test_inventory_permission_is_default_denied_and_independent_of_host_and_read(inventory_proxy, operation: str) -> None:
    h, requests, _ = inventory_proxy
    headers = {"X-BoxTeam-Federation-Token": h.federation_token}
    for capability in (None, "discovery", "read"):
        rules = [] if capability is None else [{"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]
        h.policy.publish({"rules": rules})
        assert h.client.post(PATH.rsplit("/", 1)[0] + f"/{operation}", json=_payload(h, operation), headers=headers).status_code == 403
    assert requests == []
    h.policy.publish({"rules": [{"capability": "extension_host_manage", "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})
    host_response = h.client.post(PATH.rsplit("/", 1)[0] + f"/{operation}", json=_payload(h, operation), headers=headers)
    assert host_response.status_code == (200 if operation == "inventory" else 403)
    assert len(requests) == (1 if operation == "inventory" else 0)
    h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})
    assert h.client.post(PATH.rsplit("/", 1)[0] + f"/{operation}", json=_payload(h, operation), headers=headers).status_code == 200
    assert len(requests) == (2 if operation == "inventory" else 1)


def test_install_forwards_exact_target_and_never_replays_unknown_result(inventory_proxy) -> None:
    h, requests, handlers = inventory_proxy
    path = "/api/gateway/extension-installations/install"
    payload = _payload(h, "install")
    response = h.client.post(path, json=payload, headers={"X-Local-Token": h.token, "X-Request-ID": "install-once"})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == _data(requests[0])
    assert requests[0].url.path == "/api/internal/extension-installations/install"
    assert json.loads(requests[0].content) == payload
    assert requests[0].headers["X-Request-ID"] == response.headers["X-Request-ID"] == "install-once"

    def lost_reply(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("安装可能已完成，回执丢失", request=request)

    handlers[0] = lost_reply
    failed = h.client.post(path, json=payload, headers={"X-Local-Token": h.token})
    assert failed.status_code == 502 and len(requests) == 2
    assert "回执未确认，请先查询原目标库存" in failed.json()["detail"]


@pytest.mark.parametrize("invalid", ["missing-backend", "null-backend", "duplicate", "version", "boolean-version", "view", "foreign", "mixed", "offline"])
def test_inventory_refuses_invalid_or_stopped_target_without_fallback(inventory_proxy, invalid: str) -> None:
    h, requests, _ = inventory_proxy
    payload = _payload(h)
    if invalid == "missing-backend":
        del payload["backend_id"]
    elif invalid == "null-backend":
        payload["backend_id"] = None
    elif invalid == "duplicate":
        payload["workspace_ids"] = [h.request.workspace_ids[0]] * 2
    elif invalid in {"version", "boolean-version"}:
        payload["protocol_version"] = 2 if invalid == "version" else True
    elif invalid == "view":
        payload["view_id"] = "window-a"
    elif invalid == "foreign":
        payload["gateway_id"] = PEER
    elif invalid == "mixed":
        h.registry.resolve((GATEWAY, h.request.workspace_ids[1])).backend_url = "http://127.0.0.1:41101"
    else:
        h.registry.resolve((GATEWAY, h.request.workspace_ids[0])).connection_error = "stopped"
    response = h.client.post(PATH, json=payload, headers={"X-Local-Token": h.token})
    assert response.status_code == (409 if invalid in {"foreign", "mixed", "offline"} else 422)
    assert requests == [] and "data" not in response.json()


@pytest.mark.parametrize("invalidate", ["route", "user", "policy", "expiry"])
@pytest.mark.parametrize("operation", ["inventory", "install"])
def test_inventory_rechecks_authority_before_any_disclosure(inventory_proxy, invalidate: str, operation: str) -> None:
    h, requests, handlers = inventory_proxy
    h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})

    def respond(_request: httpx.Request) -> httpx.Response:
        if invalidate == "route":
            h.registry.invalidate_route((GATEWAY, h.request.workspace_ids[0]))
        elif invalidate == "user":
            h.users.release(h.access)
        elif invalidate == "policy":
            h.policy.publish({"rules": []})
        else:
            connection = h.users._state.connection()
            try:
                connection.execute("UPDATE user_access_lease SET expires_at = ? WHERE access_session_id = ?",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), h.access.access_session_id))
            finally:
                connection.close()
        return httpx.Response(200, json={"must_not_be_disclosed": True})

    handlers[0] = respond
    headers = {"X-BoxTeam-Federation-Token": h.federation_token} if invalidate == "policy" else {"X-Local-Token": h.token}
    response = h.client.post(PATH.rsplit("/", 1)[0] + f"/{operation}", json=_payload(h, operation), headers=headers)
    assert response.status_code == (409 if invalidate == "route" else 403)
    assert "must_not_be_disclosed" not in response.text and len(requests) == 1


@pytest.mark.parametrize("wrong", ["gateway_id", "workspace_ids", "backend_id", "protocol_version", "profile_scope", "source", "validity", "request_id", "header", "offline"])
def test_inventory_never_discloses_wrong_target_or_invalid_inventory(inventory_proxy, wrong: str) -> None:
    h, requests, handlers = inventory_proxy

    def respond(request: httpx.Request) -> httpx.Response:
        if wrong == "offline":
            raise httpx.ConnectError("target stopped", request=request)
        data = _data(request)
        if wrong in {"gateway_id", "workspace_ids", "backend_id", "protocol_version", "profile_scope"}:
            data[wrong] = {"gateway_id": PEER, "workspace_ids": [str(uuid4())], "backend_id": str(uuid4()),
                "protocol_version": 2, "profile_scope": "active-window"}[wrong]
        if wrong in {"source", "validity"}:
            entry = data["inventory"]["extensions"][0]
            entry["source" if wrong == "source" else "is_valid"] = "system" if wrong == "source" else "true"
        return httpx.Response(200, headers={"X-Request-ID": "wrong" if wrong == "header" else request.headers["X-Request-ID"]},
            json={"request_id": "wrong" if wrong == "request_id" else request.headers["X-Request-ID"], "data": data})

    handlers[0] = respond
    response = h.client.post(PATH, json=_payload(h), headers={"X-Local-Token": h.token, "X-Request-ID": "wrong-target"})
    assert response.status_code == (502 if wrong in {"protocol_version", "profile_scope", "source", "validity", "offline"} else 409)
    assert response.headers["X-Request-ID"] == "wrong-target" and "data" not in response.json()
    assert len(requests) == 1


def test_inventory_preserves_target_rejection_and_original_request_id(inventory_proxy) -> None:
    h, requests, handlers = inventory_proxy
    handlers[0] = lambda request: httpx.Response(409, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
        "detail": "原 Backend 已换代", "request_id": request.headers["X-Request-ID"],
    })
    response = h.client.post(PATH, json=_payload(h), headers={"X-Local-Token": h.token, "X-Request-ID": "backend-replaced"})
    assert response.status_code == 409 and response.json() == {"detail": "原 Backend 已换代", "request_id": "backend-replaced"}
    assert response.headers["X-Request-ID"] == "backend-replaced" and len(requests) == 1


@pytest.mark.parametrize("federated", [False, True])
@pytest.mark.parametrize("operation", ["inventory", "install"])
def test_inventory_relay_uses_original_remote_owner_and_forbids_nested_relay(inventory_proxy, federated: bool, operation: str) -> None:
    h, requests, _ = inventory_proxy
    h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id="test-peer", name="remote", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))
    for workspace_id in h.request.workspace_ids:
        h.registry.upsert(WorkspaceTarget(gateway_id=PEER, workspace_id=workspace_id, name="same", root_path=None,
            backend_url="", connection_kind="remote_gateway", owner="remote_projection", remote_gateway_connection_ids=("test-peer",),
            remote_service_names=("workspace_api",)), activate=False)
    h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})
    headers = {"X-BoxTeam-Federation-Token": h.federation_token} if federated else {"X-Local-Token": h.token}
    payload = {**_payload(h, operation), "gateway_id": PEER}
    if operation == "install":
        payload["source_uri"] = payload["source_uri"].replace(GATEWAY, PEER)
    path = PATH.rsplit("/", 1)[0] + f"/{operation}"
    response = h.client.post(path, json=payload, headers={**headers, "X-Request-ID": "relay-inventory"})
    if federated:
        assert response.status_code == 403 and requests == []
    else:
        assert response.status_code == 200, response.text
        [forwarded] = requests
        assert forwarded.url.path == path and forwarded.url.host == "remote.test"
        assert json.loads(forwarded.content) == payload
        assert forwarded.headers["X-BoxTeam-Federation-Token"] == h.federation_token
        assert forwarded.headers["X-BoxTeam-Target-Gateway-Id"] == PEER
        assert "X-Local-Token" not in forwarded.headers and "cookie" not in forwarded.headers
        assert response.json()["data"] == _data(forwarded)
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "relay-inventory"
