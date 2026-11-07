"""原生管理调用与远端 HTTP 分开设边界，验证真实 Gateway 授权和路由。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, JsonValue

from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import workspaces
from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
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
NativeResult = TypeVar("NativeResult", bound=BaseModel)


def _data(request: httpx.Request) -> dict[str, object]:
    payload = json.loads(request.content)
    source = payload.pop("source_uri", None)
    uninstalled = None
    if request.url.path.endswith("/uninstall"):
        uninstalled = {"id": payload.pop("extension_id"), "version": payload.pop("version"), "profile_id": payload.pop("profile_id")}
    result = {**payload, "profile_scope": "default", "inventory": {
        "profile_id": "default-profile", "extensions": [{"source": "profile", "extension_key": "publisher.probe-1.0.0", "description": {
            "identifier": {"value": "publisher.probe"}, "version": "1.0.0",
            "extensionLocation": {"scheme": "file", "path": "/target/extensions/probe"},
            "contributes": {"commands": [{"command": "probe.run", "title": "原始贡献"}]},
        }, "is_valid": False, "validations": [{"severity": 3, "message": "原 manifest 缺失"}]}],
    }}
    if source is not None:
        result["installed"] = {"id": "publisher.probe", "version": "1.0.0", "profile_id": "default-profile", "reused": False}
    if uninstalled is not None:
        result["uninstalled"] = uninstalled
        result["inventory"]["extensions"] = []
    return result


def _reply(request: httpx.Request, data: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
        "request_id": request.headers["X-Request-ID"], "data": _data(request) if data is None else data,
    })


@dataclass
class NativeControl:
    owner: GatewayExtensionManagement
    calls: list[dict[str, JsonValue]]
    handlers: list[Callable[[dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]]]


@pytest.fixture
def native_control(host_gateway: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NativeControl:
    owner = GatewayExtensionManagement(backend_id=host_gateway.request.backend_id,
        runtime=NodeHostRuntime(tmp_path / "node", tmp_path / "extensionHostProcess.mjs", "a" * 40, "1.0.0", "24.0.0"),
        directory=tmp_path / "management", environment={}, log_dir=tmp_path / "logs",
        logging_startup={"revision": "test", "level": "info", "pretty": False})
    calls: list[dict[str, JsonValue]] = []

    async def reply(command: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if command["method"] == "extension-file-read":
            return {"result": {"content_base64": "b2xk"}}
        if command["method"] == "extension-file-stat":
            return {"result": {"type": 1, "ctime": 0, "mtime": 1, "size": 3, "permissions": 1}}
        inventory: dict[str, JsonValue] = {"profile_id": "default-profile", "extensions": [{
            "source": "profile", "extension_key": "publisher.probe-1.0.0", "description": {
                "identifier": {"value": "publisher.probe"}, "version": "1.0.0"},
            "is_valid": False, "validations": [{"severity": 3, "message": "原 manifest 缺失"}]}]}
        if command["method"] == "uninstall":
            inventory["extensions"] = []
            return {"result": {"inventory": inventory, "uninstalled": {"id": command["extension_id"],
                "profile_id": command["profile_id"], "version": command["version"]}}}
        assert command == {"method": "inventory"}
        return {"result": inventory}

    handlers: list[Callable[[dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]]] = [reply]

    async def request(command: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        calls.append(dict(command))
        return await handlers[0](dict(command))

    async def call(command: Mapping[str, JsonValue], *, response_type: type[NativeResult]) -> NativeResult:
        # 本组只验证真实 Gateway 判权与回执；原生组件在独立 HTTP 集成组验证。
        reply = await request(command)
        return response_type.model_validate(reply["result"])

    monkeypatch.setattr(owner, "request", request)
    monkeypatch.setattr(owner, "call", call)
    monkeypatch.setattr(owner, "assert_healthy", lambda: None)
    host_gateway.application.state.extension_management = owner
    return NativeControl(owner, calls, handlers)


@pytest.fixture
def inventory_proxy(host_gateway: Harness, native_control: NativeControl) -> Iterator[tuple[Harness, list[httpx.Request], list[ResponseHandler]]]:
    h = host_gateway
    requests: list[httpx.Request] = []
    handlers: list[ResponseHandler] = [_reply]
    h.registry.resolve(h.request.workspace_ids[0]).system_default = True
    for workspace_id in h.request.workspace_ids:
        h.registry.resolve(workspace_id).backend_id = h.request.backend_id

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
    result = h.request.model_dump(exclude={"view_id", "workspace_ids"})
    if operation == "install":
        result["workspace_ids"] = [h.request.workspace_ids[0]]
        result["source_uri"] = f"boxteam://{GATEWAY}/workspace/{h.request.workspace_ids[0]}/resources/files/root/probe.vsix"
    elif operation == "uninstall":
        result.update(profile_id="default-profile", extension_id="publisher.probe", version="1.0.0")
    return result


@pytest.mark.parametrize("operation", ["inventory", "uninstall"])
def test_native_management_survives_no_workspace_and_stopped_backends(inventory_proxy, native_control, operation):
    h, requests, _ = inventory_proxy
    for workspace_id in h.request.workspace_ids:
        h.registry.resolve(workspace_id).system_default = False
        h.registry.remove(workspace_id)
    response = h.client.post(f"/api/gateway/extension-installations/{operation}", json=_payload(h, operation),
        headers={"X-Local-Token": h.token, "X-Request-ID": "native-management"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["backend_id"] == native_control.owner.backend_id
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "native-management"
    assert native_control.calls == ([{"method": "inventory"}] if operation == "inventory" else [{
        "method": "uninstall", "profile_id": "default-profile", "extension_id": "publisher.probe", "version": "1.0.0"}])
    assert requests == [] and h.calls == []


def test_install_selects_stable_domain_then_complete_source_scope(inventory_proxy) -> None:
    h, requests, _ = inventory_proxy
    source = h.registry.resolve(h.request.workspace_ids[1])
    source.backend_url = "http://127.0.0.1:41102"
    payload = {**_payload(h, "install"), "workspace_ids": [source.workspace_id],
        "source_uri": f"boxteam://{GATEWAY}/workspace/{source.workspace_id}/resources/files/root/probe.vsix"}
    response = h.client.post("/api/gateway/extension-installations/install", json=payload, headers={"X-Local-Token": h.token})
    assert response.status_code == 200, response.text
    assert len(requests) == 1 and requests[0].url.port == 41102
    source.backend_id = str(uuid4())
    rejected = h.client.post("/api/gateway/extension-installations/install", json=payload, headers={"X-Local-Token": h.token})
    assert rejected.status_code == 409 and len(requests) == 1


def test_install_requires_one_original_owner_covering_all_sources(inventory_proxy) -> None:
    h, requests, _ = inventory_proxy
    members = tuple(ManagedBackendMember(workspace_id=workspace_id) for workspace_id in h.request.workspace_ids)
    for workspace_id in h.request.workspace_ids:
        h.registry.resolve(workspace_id).managed = True
    owner = ManagedBackendOwner(ManagedBackendContract(gateway_id=GATEWAY, owner_id="explicit-owner",
        generation="generation", members=members, backend_url="http://127.0.0.1:41100"))
    owner.backend_id = h.request.backend_id
    owner.set_process(Mock(is_alive=Mock(return_value=True)))
    h.registry.register_backend_owner(owner)
    payload = {**_payload(h, "install"), "workspace_ids": h.request.workspace_ids}
    response = h.client.post("/api/gateway/extension-installations/install", json=payload, headers={"X-Local-Token": h.token})
    assert response.status_code == 200, response.text
    assert len(requests) == 1 and json.loads(requests[0].content)["workspace_ids"] == h.request.workspace_ids
    foreign = WorkspaceTarget(gateway_id=GATEWAY, workspace_id=str(uuid4()), backend_id=h.request.backend_id,
        name="same-domain-but-other-route", root_path="/other", backend_url="http://127.0.0.1:41102", connection_kind="local")
    h.registry.upsert(foreign, activate=False)
    rejected = h.client.post("/api/gateway/extension-installations/install",
        json={**payload, "workspace_ids": [*h.request.workspace_ids, foreign.workspace_id]}, headers={"X-Local-Token": h.token})
    assert rejected.status_code == 409 and len(requests) == 1


def test_workspace_resolve_confirms_original_route_for_direct_install_without_target_listing(inventory_proxy) -> None:
    h, requests, handlers = inventory_proxy
    for workspace_id in h.request.workspace_ids:
        h.registry.resolve(workspace_id).backend_id = None
    source = h.registry.resolve(h.request.workspace_ids[0])
    handlers[0] = lambda request: _reply(request, {**json.loads(request.content), "backend_id": h.request.backend_id})
    resolved = h.client.post("/api/gateway/extension-targets/resolve", headers={"X-Local-Token": h.token},
        json={"protocol_version": 1, "gateway_id": GATEWAY, "workspace_ids": [source.workspace_id]})
    assert resolved.status_code == 200, resolved.text
    assert source.backend_id == resolved.json()["data"]["backend_id"] == h.request.backend_id
    assert h.registry.resolve(h.request.workspace_ids[1]).backend_id is None
    handlers[0] = _reply
    installed = h.client.post("/api/gateway/extension-installations/install", headers={"X-Local-Token": h.token},
        json=_payload(h, "install"))
    assert installed.status_code == 200, installed.text
    assert [request.url.path for request in requests] == [
        "/api/internal/extension-targets/resolve", "/api/internal/extension-installations/install"]
    assert h.calls == []

    # 运行投影只在当前原对象保留；普通 registry 保存不得引入安装域账本。
    h.registry.activate(source.key)
    path = h.registry._storage_path
    assert all("backend_id" not in target for target in json.loads(path.read_text())["targets"])
    reopened = GatewayWorkspaceRegistry(storage_path=path, gateway_id=GATEWAY)
    try:
        assert all(target.backend_id is None for target in reopened.targets())
    finally:
        reopened.close()


def test_target_directory_uses_gateway_owner_without_backend_identity(inventory_proxy, native_control):
    h, requests, _ = inventory_proxy
    for workspace_id in h.request.workspace_ids:
        h.registry.resolve(workspace_id).backend_id = None
    response = h.client.post("/api/gateway/extension-targets", json={"protocol_version": 1, "gateway_id": GATEWAY},
        headers={"X-Local-Token": h.token, "X-Request-ID": "domain-directory"})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "domain-directory"
    assert response.json()["data"] == {"protocol_version": 1, "gateway_id": GATEWAY, "source": "live", "targets": [{
        "protocol_version": 1, "gateway_id": GATEWAY, "backend_id": h.request.backend_id, "source": "live",
        "capabilities": {"installation_manage": True, "host_manage": True}}]}
    assert all(h.registry.resolve(workspace_id).backend_id is None for workspace_id in h.request.workspace_ids)
    assert requests == [] and native_control.calls == [] and h.calls == []


def test_management_directory_preserves_remote_connections_without_workspaces(inventory_proxy) -> None:
    h, requests, _ = inventory_proxy
    h.application.include_router(workspaces.router)
    for connection_id in ("first-route", "second-route"):
        h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id=connection_id, name=connection_id,
            host="remote.test", port=22, username="unit", private_key_path=None, ssh_config_host=None,
            remote_gateway_port=8014, remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"))
    response = h.client.get("/api/gateway/management/gateways", headers={"X-Local-Token": h.token})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["local_gateway_id"] == GATEWAY
    assert [(item["gateway_id"], item["gateway_connection_id"]) for item in data["items"]] == [
        (GATEWAY, None), (PEER, "first-route"), (PEER, "second-route")]
    assert requests == []


@pytest.mark.parametrize("capability", [None, "discovery", "read", "extension_installation_manage", "extension_host_manage"])
def test_target_directory_checks_native_management_permission_before_identity(inventory_proxy, capability: str | None) -> None:
    h, requests, _ = inventory_proxy
    h.policy.publish({"rules": [] if capability is None else [{"capability": capability,
        "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})
    response = h.client.post("/api/gateway/extension-targets", json={"protocol_version": 1, "gateway_id": GATEWAY},
        headers={"X-BoxTeam-Federation-Token": h.federation_token})
    permitted = capability in {"extension_installation_manage", "extension_host_manage"}
    assert response.status_code == (200 if permitted else 403), response.text
    assert requests == [] and h.calls == []


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("operation", ["inventory", "targets"])
def test_inventory_remote_route_requires_explicit_connection_without_workspace_projection(inventory_proxy, explicit: bool, operation: str) -> None:
    h, requests, handlers = inventory_proxy
    for connection_id in ("test-peer", "second-peer"):
        h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id=connection_id, name="same-name",
            host="remote.test", port=22, username="unit", private_key_path=None, ssh_config_host=None,
            remote_gateway_port=8014, remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))
    payload = ({"protocol_version": 1, "gateway_id": PEER} if operation == "targets" else {**_payload(h), "gateway_id": PEER})
    if operation == "targets":
        handlers[0] = lambda request: _reply(request, {"protocol_version": 1, "gateway_id": PEER, "source": "live", "targets": [{
            "protocol_version": 1, "gateway_id": PEER, "backend_id": h.request.backend_id, "source": "live",
            "capabilities": {"installation_manage": True, "host_manage": False}}]})
    if explicit:
        payload["gateway_connection_id"] = "test-peer"
    response = h.client.post("/api/gateway/extension-targets" if operation == "targets" else PATH,
        json=payload, headers={"X-Local-Token": h.token})
    assert response.status_code == (200 if explicit else 409), response.text
    assert len(requests) == (1 if explicit else 0)
    if explicit:
        assert "gateway_connection_id" not in json.loads(requests[0].content)
        assert all(target.gateway_id == GATEWAY for target in h.registry.targets())


def test_inventory_hides_reply_when_remote_connection_is_replaced_without_workspace_projection(inventory_proxy) -> None:
    h, requests, handlers = inventory_proxy
    connection = RemoteGatewayConnection(connection_id="test-peer", name="remote", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual")
    h.registry.upsert_remote_gateway(connection, runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))

    def replace_connection(request: httpx.Request) -> httpx.Response:
        h.registry.upsert_remote_gateway(connection, runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://new-remote.test"}))
        return _reply(request)

    handlers[0] = replace_connection
    response = h.client.post(PATH, json={**_payload(h), "gateway_id": PEER}, headers={"X-Local-Token": h.token})
    assert response.status_code == 409 and "data" not in response.json() and len(requests) == 1
    assert h.registry._remote_gateway_reference_counts == {}


def test_inventory_requires_token_and_user_lease_before_transport(inventory_proxy) -> None:
    h, requests, _ = inventory_proxy
    assert h.client.post(PATH, json=_payload(h)).status_code == 401
    h.client.cookies.clear()
    assert h.client.post(PATH, json=_payload(h), headers={"X-Local-Token": h.token}).status_code == 401
    assert requests == []


@pytest.mark.parametrize("operation", ["inventory", "install", "uninstall"])
def test_inventory_permission_is_default_denied_and_independent_of_host_and_read(inventory_proxy, native_control, operation: str) -> None:
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
    assert len(requests) + len(native_control.calls) == (1 if operation == "inventory" else 0)
    h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})
    assert h.client.post(PATH.rsplit("/", 1)[0] + f"/{operation}", json=_payload(h, operation), headers=headers).status_code == 200
    assert len(requests) + len(native_control.calls) == (2 if operation == "inventory" else 1)


@pytest.fixture
def remote_inventory(inventory_proxy):
    h, requests, handlers = inventory_proxy
    h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id="test-peer", name="remote", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))
    return h, requests, handlers


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_installation_never_replays_unknown_remote_result(remote_inventory, operation):
    h, requests, handlers = remote_inventory
    payload = {**_payload(h, operation), "gateway_id": PEER}
    if operation == "install":
        payload["source_uri"] = payload["source_uri"].replace(GATEWAY, PEER)
    path = f"/api/gateway/extension-installations/{operation}"
    response = h.client.post(path, json=payload, headers={"X-Local-Token": h.token, "X-Request-ID": "install-once"})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == _data(requests[0])
    assert requests[0].url.path == path and json.loads(requests[0].content) == payload
    assert requests[0].headers["X-Request-ID"] == response.headers["X-Request-ID"] == "install-once"

    def lost_reply(request):
        raise httpx.ReadTimeout("安装可能已完成，回执丢失", request=request)

    handlers[0] = lost_reply
    failed = h.client.post(path, json=payload, headers={"X-Local-Token": h.token})
    assert failed.status_code == 502 and len(requests) == 2
    assert "回执未确认，请先查询原目标库存" in failed.json()["detail"]


@pytest.mark.parametrize("wrong", ["id", "version", "profile_id", "inventory-profile"])
def test_uninstall_rejects_receipt_for_another_profile_member(inventory_proxy, native_control, wrong):
    h, requests, _ = inventory_proxy
    original = native_control.handlers[0]

    async def respond(command):
        reply = await original(command)
        data = reply["result"]
        if wrong == "inventory-profile":
            data["inventory"]["profile_id"] = "other-profile"
        else:
            data["uninstalled"][wrong] = {"id": "other.extension", "version": "2.0.0", "profile_id": "other-profile"}[wrong]
        return reply

    native_control.handlers[0] = respond
    response = h.client.post(PATH.rsplit("/", 1)[0] + "/uninstall", json=_payload(h, "uninstall"), headers={"X-Local-Token": h.token})
    assert response.status_code == 409 and "原 profile 成员不匹配" in response.json()["detail"]
    assert len(native_control.calls) == 1 and requests == [] and "uninstalled" not in response.json()


@pytest.mark.parametrize("invalid", ["missing-backend", "null-backend", "duplicate", "version", "boolean-version", "view", "foreign", "wrong-domain", "unconfigured", "failed"])
def test_inventory_refuses_invalid_or_stopped_target_without_fallback(inventory_proxy, native_control, monkeypatch, invalid: str) -> None:
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
    elif invalid == "wrong-domain":
        payload["backend_id"] = str(uuid4())
    elif invalid == "unconfigured":
        h.application.state.extension_management = None
    else:
        def failed():
            raise RuntimeError("管理组件异常退出")
        monkeypatch.setattr(native_control.owner, "assert_healthy", failed)
    response = h.client.post(PATH, json=payload, headers={"X-Local-Token": h.token})
    assert response.status_code == (503 if invalid == "unconfigured" else 409 if invalid in {"foreign", "wrong-domain", "failed"} else 422)
    assert requests == [] and native_control.calls == [] and "data" not in response.json()



@pytest.mark.parametrize("invalidate", ["user", "policy", "expiry"])
@pytest.mark.parametrize("operation", ["inventory", "install", "uninstall"])
def test_inventory_rechecks_authority_before_any_disclosure(inventory_proxy, native_control, invalidate: str, operation: str) -> None:
    h, requests, handlers = inventory_proxy
    h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}]})

    def respond(_request: httpx.Request | None) -> httpx.Response:
        if invalidate == "user":
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

    original_native = native_control.handlers[0]

    async def native_reply(command):
        respond(None)
        return await original_native(command)

    native_control.handlers[0] = native_reply
    handlers[0] = respond
    headers = {"X-BoxTeam-Federation-Token": h.federation_token} if invalidate == "policy" else {"X-Local-Token": h.token}
    response = h.client.post(PATH.rsplit("/", 1)[0] + f"/{operation}", json=_payload(h, operation), headers=headers)
    assert response.status_code == 403
    assert "must_not_be_disclosed" not in response.text and len(requests) + len(native_control.calls) == 1


@pytest.mark.parametrize("wrong", ["gateway_id", "workspace_ids", "backend_id", "protocol_version", "profile_scope", "source", "validity", "request_id", "header", "offline"])
def test_inventory_never_discloses_wrong_target_or_invalid_inventory(remote_inventory, wrong: str) -> None:
    h, requests, handlers = remote_inventory

    def respond(request: httpx.Request) -> httpx.Response:
        if wrong == "offline":
            raise httpx.ConnectError("target stopped", request=request)
        data = _data(request)
        if wrong in {"gateway_id", "workspace_ids", "backend_id", "protocol_version", "profile_scope"}:
            data[wrong] = {"gateway_id": GATEWAY, "workspace_ids": [str(uuid4())], "backend_id": str(uuid4()),
                "protocol_version": 2, "profile_scope": "active-window"}[wrong]
        if wrong in {"source", "validity"}:
            inventory = data["inventory"]
            assert isinstance(inventory, dict)
            entry = inventory["extensions"][0]
            entry["source" if wrong == "source" else "is_valid"] = "system" if wrong == "source" else "true"
        return httpx.Response(200, headers={"X-Request-ID": "wrong" if wrong == "header" else request.headers["X-Request-ID"]},
            json={"request_id": "wrong" if wrong == "request_id" else request.headers["X-Request-ID"], "data": data})

    handlers[0] = respond
    response = h.client.post(PATH, json={**_payload(h), "gateway_id": PEER}, headers={"X-Local-Token": h.token, "X-Request-ID": "wrong-target"})
    assert response.status_code == (502 if wrong in {"workspace_ids", "protocol_version", "profile_scope", "source", "validity", "offline"} else 409)
    assert response.headers["X-Request-ID"] == "wrong-target" and "data" not in response.json()
    assert len(requests) == 1


def test_inventory_preserves_target_rejection_and_original_request_id(remote_inventory) -> None:
    h, requests, handlers = remote_inventory
    handlers[0] = lambda request: httpx.Response(409, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
        "detail": "原 Backend 已换代", "request_id": request.headers["X-Request-ID"],
    })
    response = h.client.post(PATH, json={**_payload(h), "gateway_id": PEER}, headers={"X-Local-Token": h.token, "X-Request-ID": "backend-replaced"})
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
