"""认证与失效经过真实 Gateway 路由；只替换目标网络传输。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app.core.path_utils import get_gateway_root
from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import extension_host
from app.gateway.server.extension_host import _upstream as real_upstream
from app.schemas.gateway.extension_host import (
    ExtensionHostConfigurationResourceDTO,
    ExtensionHostConnectionDTO,
    ExtensionHostEnvironmentDTO,
    ExtensionHostFolderDTO,
    ExtensionHostInitializationDTO,
    ExtensionHostOpenRequest,
    ExtensionHostWorkspaceDTO,
)
from app.services.infrastructure.config.source_uri import extension_configuration_uri

GATEWAY = "gateway_" + "a" * 32
PEER = "gateway_" + "b" * 32
PATH = "/api/gateway/extension-hosts/channel"


@dataclass
class Harness:
    client: TestClient
    registry: GatewayWorkspaceRegistry
    users: UserAccessService
    access: UserAccessContext
    policy: FederationPolicyStore
    request: ExtensionHostOpenRequest
    calls: list[dict[str, str]]
    token: str
    federation_token: str
    application: FastAPI
    initialization: Callable[[ExtensionHostOpenRequest], ExtensionHostInitializationDTO]


@pytest.fixture
def host_initialization(tmp_path: Path) -> Callable[[ExtensionHostOpenRequest], ExtensionHostInitializationDTO]:
    def build(payload: ExtensionHostOpenRequest) -> ExtensionHostInitializationDTO:
        profile_id = "test-profile"
        configuration = ExtensionHostConfigurationResourceDTO(
            native_uri=(tmp_path / "view/workspace.code-workspace").as_uri(),
            resource_uri=extension_configuration_uri(gateway_id=payload.gateway_id,
                backend_id=payload.backend_id, workspace_id="explicit-view"),
        ) if len(payload.workspace_ids) > 1 else None
        return ExtensionHostInitializationDTO(os="linux", parent_pid=123,
            environment=ExtensionHostEnvironmentDTO(app_root_uri=(tmp_path / "component").as_uri(),
                global_storage_home_uri=(tmp_path / "globalStorage").as_uri(),
                workspace_storage_home_uri=(tmp_path / "workspaceStorage").as_uri(), profile_id=profile_id,
                remote_settings=ExtensionHostConfigurationResourceDTO(native_uri=(tmp_path / "profile/settings.json").as_uri(),
                    resource_uri=extension_configuration_uri(gateway_id=payload.gateway_id,
                        backend_id=payload.backend_id, profile_id=profile_id))),
            logs_location_uri=(tmp_path / "logs").as_uri(),
            workspace=ExtensionHostWorkspaceDTO(id="explicit-view", name="View", folders=[
                ExtensionHostFolderDTO(workspace_id=workspace_id, name="Workspace", native_uri=(tmp_path / workspace_id).as_uri(),
                    resource_uri=resource_display_uri(scope="workspace", scope_id=workspace_id, kind="files",
                        tail_segments=("root",))) for workspace_id in payload.workspace_ids], configuration=configuration))
    return build


@pytest.mark.parametrize("wrong", [None, "qualified", "foreign_gateway", "foreign_workspace", "child"])
def test_host_binding_uses_original_display_root_and_outer_gateway(
    host_initialization: Callable[[ExtensionHostOpenRequest], ExtensionHostInitializationDTO], wrong: str | None,
) -> None:
    workspace_id = str(uuid4())
    request = ExtensionHostOpenRequest(protocol_version=1, gateway_id=GATEWAY, backend_id=str(uuid4()),
        view_id="default", workspace_ids=[workspace_id])
    payload = dict(request.model_dump(), client_id="window", generation="generation", request_id="request",
        initialization=host_initialization(request).model_dump())
    if wrong is None:
        result = ExtensionHostConnectionDTO.model_validate(payload)
        assert result.gateway_id == GATEWAY
        assert result.initialization.workspace.folders[0].resource_uri == f"boxteam://workspace/{workspace_id}/resources/files/root"
        return
    payload["initialization"]["workspace"]["folders"][0]["resource_uri"] = resource_display_uri(
        scope="workspace", scope_id=str(uuid4()) if wrong == "foreign_workspace" else workspace_id, kind="files",
        tail_segments=("root", "child") if wrong == "child" else ("root",),
        gateway_authority=GATEWAY if wrong == "qualified" else PEER if wrong == "foreign_gateway" else None)
    with pytest.raises(ValueError, match="工作区根"):
        ExtensionHostConnectionDTO.model_validate(payload)


@pytest.fixture
def host_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    host_initialization: Callable[[ExtensionHostOpenRequest], ExtensionHostInitializationDTO],
) -> Iterator[Harness]:
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", gateway_id=GATEWAY)
    workspaces = [str(uuid4()), str(uuid4())]
    for workspace_id in workspaces:
        registry.upsert(WorkspaceTarget(gateway_id=GATEWAY, workspace_id=workspace_id, name="same",
            root_path=str(tmp_path / workspace_id), backend_url="http://127.0.0.1:41100", connection_kind="local"),
            activate=False)
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    users = UserAccessService(state=state)
    users.create_user(display_name="Host test", user_id="host-test")
    access = users.acquire_user(user_id="host-test", client_label="window", takeover=False)
    policy = FederationPolicyStore()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.state.registry = registry
    application.state.user_access_service = users
    application.state.federation_policy_store = policy
    application.include_router(extension_host.router)
    calls: list[dict[str, str]] = []

    async def upstream(websocket, *, url, headers, payload, **_):
        calls.append({**headers, "url": url})
        result = ExtensionHostConnectionDTO(**payload.model_dump(), client_id=headers[extension_host.CLIENT_HEADER],
            generation=str(uuid4()), request_id=headers["X-Request-ID"], initialization=host_initialization(payload))
        await websocket.send_text(result.model_dump_json())
        await asyncio.Event().wait()

    monkeypatch.setattr(extension_host, "_upstream", upstream)
    federation = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
        connection_id="test-peer", peer_gateway_id=PEER)
    with TestClient(application) as client:
        client.cookies.set(USER_ACCESS_COOKIE_NAME, access.access_session_id)
        try:
            yield Harness(client, registry, users, access, policy, ExtensionHostOpenRequest.model_validate({
                "protocol_version": 1, "gateway_id": GATEWAY, "backend_id": str(uuid4()),
                "view_id": "window-a", "workspace_ids": workspaces,
            }), calls, get_gateway_local_token(), federation.token, application, host_initialization)
        finally:
            registry.close()
            state.close()


def test_requires_local_token_and_user_lease(host_gateway: Harness) -> None:
    h = host_gateway
    with pytest.raises(WebSocketDenialResponse) as missing_token, h.client.websocket_connect(PATH):
        pass
    assert missing_token.value.status_code == 401
    h.client.cookies.clear()
    with pytest.raises(WebSocketDenialResponse) as missing_user, h.client.websocket_connect(f"{PATH}?token={h.token}"):
        pass
    assert missing_user.value.status_code == 401
    assert h.calls == []


@pytest.mark.parametrize("invalidate", ["route", "user"])
def test_idle_invalidation_releases_all_routes(host_gateway: Harness, invalidate: str) -> None:
    h = host_gateway
    with h.client.websocket_connect(f"{PATH}?token={h.token}", headers={
        "X-Request-ID": "host-request", extension_host.CLIENT_HEADER: "forged-client"}) as socket:
        socket.send_json(h.request.model_dump())
        binding = socket.receive_json()
        assert binding["request_id"] == "host-request"
        assert socket.extra_headers is not None
        assert dict(socket.extra_headers)[b"x-request-id"] == b"host-request"
        assert binding["client_id"] not in {"forged-client", h.access.access_session_id}
        assert len(binding["client_id"]) == 64
        assert h.calls[0]["X-Local-Token"] == h.token
        assert all("cookie" not in name.lower() for name in h.calls[0])
        for workspace_id in h.request.workspace_ids:
            assert h.registry.route_reference_counts((GATEWAY, workspace_id)) == (0, 1)
        assert h.client.portal is not None
        if invalidate == "route":
            h.client.portal.call(h.registry.invalidate_route, (GATEWAY, h.request.workspace_ids[1]))
        else:
            h.client.portal.call(h.users.release, h.access)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1011
    for workspace_id in h.request.workspace_ids:
        assert h.registry.route_reference_counts((GATEWAY, workspace_id)) == (0, 0)


@pytest.mark.parametrize("change", ["unknown-gateway", "duplicate-workspace", "version", "boolean-version", "client-id", "mixed-backend"])
def test_bad_open_request_never_reaches_target(host_gateway: Harness, change: str) -> None:
    h = host_gateway
    payload = h.request.model_dump()
    if change == "unknown-gateway":
        payload["gateway_id"] = PEER
    elif change == "duplicate-workspace":
        payload["workspace_ids"] = [h.request.workspace_ids[0]] * 2
    elif change == "version":
        payload["protocol_version"] = 2
    elif change == "boolean-version":
        payload["protocol_version"] = True
    elif change == "client-id":
        payload["client_id"] = "forged"
    else:
        second = h.registry.resolve((GATEWAY, h.request.workspace_ids[1]))
        second.backend_url = "http://127.0.0.1:41101"
    with h.client.websocket_connect(f"{PATH}?token={h.token}") as socket:
        socket.send_json(payload)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert h.calls == []
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


@pytest.mark.parametrize("field", ["client_id", "request_id", "backend_id", "workspace_ids"])
def test_wrong_target_handshake_is_never_disclosed(host_gateway: Harness, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    h = host_gateway
    monkeypatch.setattr(extension_host, "_upstream", real_upstream)

    class Upstream:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers
            self.request = h.request

        async def send(self, raw: str) -> None:
            self.request = ExtensionHostOpenRequest.model_validate_json(raw)

        async def recv(self) -> str:
            binding = {**self.request.model_dump(), "client_id": self.headers[extension_host.CLIENT_HEADER],
                       "generation": str(uuid4()), "request_id": self.headers["X-Request-ID"]}
            binding[field] = [str(uuid4())] if field == "workspace_ids" else str(uuid4())
            # workspace 身份替换仍构成合法的另一视图，由真实 Gateway 核原请求归属。
            target = ExtensionHostOpenRequest.model_validate({name: binding[name] for name in ExtensionHostOpenRequest.model_fields})
            binding["initialization"] = h.initialization(target).model_dump()
            return json.dumps(binding)

    @asynccontextmanager
    async def connect(_url: str, *, additional_headers: dict[str, str], **_):
        yield Upstream(additional_headers)

    monkeypatch.setattr(extension_host, "connect", connect)
    with h.client.websocket_connect(f"{PATH}?token={h.token}") as socket:
        socket.send_json(h.request.model_dump())
        with pytest.raises(WebSocketDisconnect) as rejected:
            socket.receive_json()
        assert rejected.value.code == 1011
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_native_federation_permission_and_idle_revocation(host_gateway: Harness) -> None:
    h = host_gateway
    headers = {"X-BoxTeam-Federation-Token": h.federation_token, extension_host.CLIENT_HEADER: "origin-client"}
    with pytest.raises(WebSocketDenialResponse) as denied, h.client.websocket_connect(PATH, headers=headers):
        pass
    assert denied.value.status_code == 403
    assert h.calls == []
    h.policy.publish({"rules": [{"capability": "extension_host_manage", "effect": "allow",
        "principal_ref": principal_ref_for_gateway(PEER)}]})
    with h.client.websocket_connect(PATH, headers=headers) as socket:
        socket.send_json(h.request.model_dump())
        binding = socket.receive_json()
        assert binding["client_id"] == f"peer:{PEER}:origin-client"
        assert h.client.portal is not None
        h.client.portal.call(h.policy.publish, {"rules": []})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_idle_channel_expires_without_renewing_user_lease(host_gateway: Harness) -> None:
    h = host_gateway
    # 时间边界装具只缩短原租约；解析、到期检查与关闭仍走原产品入口。
    expires = (datetime.now(UTC) + timedelta(seconds=0.2)).isoformat()
    connection = h.users._state.connection()
    try:
        connection.execute("UPDATE user_access_lease SET expires_at = ? WHERE access_session_id = ?",
                           (expires, h.access.access_session_id))
    finally:
        connection.close()
    with h.client.websocket_connect(f"{PATH}?token={h.token}") as socket:
        socket.send_json(h.request.model_dump())
        assert socket.receive_json()["view_id"] == h.request.view_id
        with pytest.raises(WebSocketDisconnect) as expired:
            socket.receive_json()
        assert expired.value.code == 1011
    assert h.users.expires_at(h.access) == expires
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_public_shapes_follow_descriptor() -> None:
    from app.protocol.generated.boxteam.gateway.v2 import public_pb2
    from app.protocol.schema import assert_public_schema

    for model in (ExtensionHostOpenRequest, ExtensionHostConnectionDTO):
        assert_public_schema(model.model_json_schema(), public_pb2.DESCRIPTOR.message_types_by_name[model.__name__])


@pytest.fixture
def target_discovery(host_gateway: Harness) -> Iterator[tuple[Harness, list[httpx.Request]]]:
    h = host_gateway
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/extension-targets/resolve"):
            data = json.loads(request.content)
            data["backend_id"] = h.request.backend_id
            return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]},
                json={"request_id": request.headers["X-Request-ID"], "data": data})
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
            "request_id": request.headers["X-Request-ID"], "data": {
                "protocol_version": 1, "gateway_id": GATEWAY, "backend_id": h.request.backend_id,
                "workspace_ids": h.request.workspace_ids, "source": "live", "platform": "linux",
                "channel_state": "unavailable", "reason": "runtime_missing", "runtime": None,
            }})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    h.application.state.http_client = upstream
    try:
        yield h, requests
    finally:
        assert h.client.portal is not None
        h.client.portal.call(upstream.aclose)


def test_discovery_binds_native_backend_without_trusting_client_identity(target_discovery: tuple[Harness, list[httpx.Request]]) -> None:
    h, requests = target_discovery
    payload = h.request.model_dump(exclude={"view_id", "backend_id"})
    response = h.client.post("/api/gateway/extension-hosts/resolve", json=payload, headers={
        "X-Local-Token": h.token, "X-Request-ID": "discover-target", extension_host.CLIENT_HEADER: "forged"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "discover-target"
    assert response.json()["data"]["backend_id"] == h.request.backend_id
    assert response.json()["data"]["source"] == "live"
    assert len(requests) == 1 and requests[0].url.path == "/api/internal/extension-hosts/resolve"
    assert requests[0].headers["X-BoxTeam-Gateway-Id"] == GATEWAY
    assert requests[0].headers["X-Local-Token"] == h.token
    assert "cookie" not in requests[0].headers and extension_host.CLIENT_HEADER not in requests[0].headers
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


@pytest.mark.parametrize("invalid", ["version", "duplicate", "foreign", "mixed", "offline", "backend"])
def test_discovery_refuses_invalid_or_reused_target(target_discovery: tuple[Harness, list[httpx.Request]], invalid: str) -> None:
    h, requests = target_discovery
    payload = h.request.model_dump(exclude={"view_id"})
    if invalid == "version":
        payload["protocol_version"] = 2
    elif invalid == "duplicate":
        payload["workspace_ids"] = [h.request.workspace_ids[0]] * 2
    elif invalid == "foreign":
        payload["gateway_id"] = PEER
    elif invalid == "mixed":
        h.registry.resolve((GATEWAY, h.request.workspace_ids[1])).backend_url = "http://127.0.0.1:41101"
    elif invalid == "offline":
        h.registry.resolve((GATEWAY, h.request.workspace_ids[0])).connection_error = "stopped"
    else:
        payload["backend_id"] = str(uuid4())
    response = h.client.post("/api/gateway/extension-hosts/resolve", json=payload, headers={"X-Local-Token": h.token})
    assert response.status_code == (422 if invalid in {"version", "duplicate"} else 409)
    assert "data" not in response.json()
    assert len(requests) == (1 if invalid == "backend" else 0)
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


@pytest.mark.parametrize("invalidate", ["route", "user", "policy"])
def test_discovery_rechecks_authority_before_disclosing(target_discovery: tuple[Harness, list[httpx.Request]], invalidate: str) -> None:
    h, requests = target_discovery
    principal = principal_ref_for_gateway(PEER)
    h.policy.publish({"rules": [{"capability": "extension_host_manage", "effect": "allow", "principal_ref": principal}]})

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if invalidate == "route":
            h.registry.invalidate_route((GATEWAY, h.request.workspace_ids[0]))
        elif invalidate == "user":
            h.users.release(h.access)
        else:
            h.policy.publish({"rules": []})
        return httpx.Response(200, json={"must_not_be_disclosed": True})

    h.application.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    headers = {"X-BoxTeam-Federation-Token": h.federation_token} if invalidate == "policy" else {"X-Local-Token": h.token}
    try:
        response = h.client.post("/api/gateway/extension-hosts/resolve", json=h.request.model_dump(exclude={"view_id"}), headers=headers)
        assert response.status_code == (409 if invalidate == "route" else 403)
        assert "must_not_be_disclosed" not in response.text
        assert len(requests) == 1
    finally:
        assert h.client.portal is not None
        h.client.portal.call(h.application.state.http_client.aclose)
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)


def test_discovery_uses_independent_native_permission(target_discovery: tuple[Harness, list[httpx.Request]]) -> None:
    h, requests = target_discovery
    headers = {"X-BoxTeam-Federation-Token": h.federation_token}
    for capability in ("discovery", "read", "extension_installation_manage"):
        h.policy.publish({"rules": [{"capability": capability, "effect": "allow",
                                    "principal_ref": principal_ref_for_gateway(PEER)}]})
        response = h.client.post("/api/gateway/extension-hosts/resolve", json=h.request.model_dump(exclude={"view_id"}), headers=headers)
        assert response.status_code == 403
    assert requests == []
    h.policy.publish({"rules": [{"capability": "extension_host_manage", "effect": "allow",
                                "principal_ref": principal_ref_for_gateway(PEER)}]})
    response = h.client.post("/api/gateway/extension-hosts/resolve", json=h.request.model_dump(exclude={"view_id"}), headers=headers)
    assert response.status_code == 200 and len(requests) == 1


def test_discovery_requires_local_token_and_user_lease(target_discovery: tuple[Harness, list[httpx.Request]]) -> None:
    h, requests = target_discovery
    payload = h.request.model_dump(exclude={"view_id"})
    assert h.client.post("/api/gateway/extension-hosts/resolve", json=payload).status_code == 401
    h.client.cookies.clear()
    assert h.client.post("/api/gateway/extension-hosts/resolve", json=payload, headers={"X-Local-Token": h.token}).status_code == 401
    assert not requests


def test_extension_target_resolve_requires_either_exact_management_capability_before_backend(
    target_discovery: tuple[Harness, list[httpx.Request]],
) -> None:
    h, requests = target_discovery
    headers = {"X-BoxTeam-Federation-Token": h.federation_token}
    principal = principal_ref_for_gateway(PEER)
    for rules in ([],
        [{"capability": "discovery", "effect": "allow", "principal_ref": principal}],
        [{"capability": "read", "effect": "allow", "principal_ref": principal}],
        [{"capability": "managed_workspace_admin", "effect": "allow", "principal_ref": principal}],
    ):
        h.policy.publish({"rules": rules})
        response = h.client.post("/api/gateway/extension-targets/resolve",
            json=h.request.model_dump(exclude={"view_id"}), headers=headers)
        assert response.status_code == 403 and requests == []

    for index, (capability, expected) in enumerate((("extension_installation_manage", (True, False)),
                                 ("extension_host_manage", (False, True))), start=1):
        h.policy.publish({"rules": [{"capability": capability, "effect": "allow", "principal_ref": principal}]})
        response = h.client.post("/api/gateway/extension-targets/resolve",
            json=h.request.model_dump(exclude={"view_id"}), headers={**headers, "X-Request-ID": capability})
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == capability
        assert response.json()["data"] == {**h.request.model_dump(exclude={"view_id"}),
            "source": "live", "capabilities": {"installation_manage": expected[0], "host_manage": expected[1]}}
        assert len(requests) == index

    assert all(h.registry.route_reference_counts((GATEWAY, workspace_id)) == (0, 0)
        for workspace_id in h.request.workspace_ids)


@pytest.mark.parametrize("wrong", ["gateway_id", "workspace_ids", "backend_id", "request_id", "capabilities"])
def test_extension_target_remote_rejects_wrong_identity_or_no_capability(
    target_discovery: tuple[Harness, list[httpx.Request]], wrong: str,
) -> None:
    h, _ = target_discovery
    h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id="test-peer", name="remote", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))
    for workspace_id in h.request.workspace_ids:
        h.registry.upsert(WorkspaceTarget(gateway_id=PEER, workspace_id=workspace_id, name="same", root_path=None,
            backend_url="", connection_kind="remote_gateway", owner="remote_projection", remote_gateway_connection_ids=("test-peer",),
            remote_service_names=("workspace_api",)), activate=False)
    principal = principal_ref_for_gateway(PEER)
    h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow", "principal_ref": principal}]})

    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        data = {"protocol_version": 1, "gateway_id": PEER, "workspace_ids": h.request.workspace_ids,
            "backend_id": h.request.backend_id, "source": "live",
            "capabilities": {"installation_manage": True, "host_manage": False}}
        if wrong == "gateway_id": data[wrong] = GATEWAY
        if wrong == "workspace_ids": data[wrong] = [str(uuid4())]
        if wrong == "backend_id": data[wrong] = str(uuid4())
        if wrong == "capabilities": data[wrong] = {"installation_manage": False, "host_manage": False}
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]},
            json={"request_id": "invalid" if wrong == "request_id" else request.headers["X-Request-ID"], "data": data})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    h.application.state.http_client = client
    try:
        payload = {**h.request.model_dump(exclude={"view_id"}), "gateway_id": PEER}
        response = h.client.post("/api/gateway/extension-targets/resolve", json=payload,
            headers={"X-Local-Token": h.token, "X-Request-ID": "remote-target"})
        assert response.status_code == (502 if wrong == "capabilities" else 409)
        assert "data" not in response.json() and len(seen) == 1
    finally:
        assert h.client.portal is not None
        h.client.portal.call(client.aclose)


def test_extension_target_relay_preserves_native_capabilities(target_discovery: tuple[Harness, list[httpx.Request]]) -> None:
    h, _ = target_discovery
    h.registry.upsert_remote_gateway(RemoteGatewayConnection(connection_id="test-peer", name="remote", host="remote.test", port=22,
        username="unit", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=PEER, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual"),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))
    for workspace_id in h.request.workspace_ids:
        h.registry.upsert(WorkspaceTarget(gateway_id=PEER, workspace_id=workspace_id, name="same", root_path=None,
            backend_url="", connection_kind="remote_gateway", owner="remote_projection", remote_gateway_connection_ids=("test-peer",),
            remote_service_names=("workspace_api",)), activate=False)
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
            "request_id": request.headers["X-Request-ID"], "data": {
                "protocol_version": 1, "gateway_id": PEER, "workspace_ids": h.request.workspace_ids,
                "backend_id": h.request.backend_id, "source": "live",
                "capabilities": {"installation_manage": False, "host_manage": True},
            },
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    h.application.state.http_client = client
    try:
        payload = {**h.request.model_dump(exclude={"view_id"}), "gateway_id": PEER}
        response = h.client.post("/api/gateway/extension-targets/resolve", json=payload,
            headers={"X-Local-Token": h.token, "X-Request-ID": "relay-target"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["capabilities"] == {"installation_manage": False, "host_manage": True}
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "relay-target"
        assert len(seen) == 1 and seen[0].url.path == "/api/gateway/extension-targets/resolve"
        assert "X-BoxTeam-Federation-Token" in seen[0].headers
    finally:
        assert h.client.portal is not None
        h.client.portal.call(client.aclose)


def test_extension_target_revocation_hides_backend_error_before_relay(target_discovery: tuple[Harness, list[httpx.Request]]) -> None:
    h, requests = target_discovery
    principal = principal_ref_for_gateway(PEER)
    h.policy.publish({"rules": [{"capability": "extension_installation_manage", "effect": "allow", "principal_ref": principal}]})

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        h.policy.publish({"rules": []})
        return httpx.Response(502, headers={"X-Request-ID": request.headers["X-Request-ID"]}, json={
            "detail": "must not be disclosed", "request_id": request.headers["X-Request-ID"],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    h.application.state.http_client = client
    try:
        response = h.client.post("/api/gateway/extension-targets/resolve",
            json=h.request.model_dump(exclude={"view_id"}),
            headers={"X-BoxTeam-Federation-Token": h.federation_token, "X-Request-ID": "revoked-target"})
        assert response.status_code == 403 and "must not be disclosed" not in response.text
        assert len(requests) == 1
    finally:
        assert h.client.portal is not None
        h.client.portal.call(client.aclose)


@pytest.mark.parametrize("wrong", ["gateway_id", "workspace_ids", "protocol_version", "source", "request_id", "header", "available", "offline"])
def test_discovery_never_discloses_wrong_or_cached_upstream(target_discovery: tuple[Harness, list[httpx.Request]], wrong: str) -> None:
    h, requests = target_discovery

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if wrong == "offline":
            raise httpx.ConnectError("target stopped", request=request)
        data = {"protocol_version": 1, "gateway_id": GATEWAY, "workspace_ids": h.request.workspace_ids,
                "backend_id": h.request.backend_id, "source": "live", "platform": "linux",
                "channel_state": "unavailable", "reason": "runtime_missing"}
        if wrong in {"gateway_id", "source", "workspace_ids", "protocol_version"}:
            data[wrong] = {"gateway_id": PEER, "source": "mirror", "workspace_ids": [str(uuid4())], "protocol_version": 2}[wrong]
        elif wrong == "available":
            data.update(channel_state="available", reason=None)
        return httpx.Response(200, headers={"X-Request-ID": "wrong" if wrong == "header" else request.headers["X-Request-ID"]},
            json={"request_id": "wrong" if wrong == "request_id" else request.headers["X-Request-ID"], "data": data})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    h.application.state.http_client = upstream
    try:
        response = h.client.post("/api/gateway/extension-hosts/resolve", json=h.request.model_dump(exclude={"view_id"}),
                                 headers={"X-Local-Token": h.token})
        assert response.status_code == (502 if wrong in {"source", "protocol_version", "available", "offline"} else 409)
        assert "data" not in response.json()
        assert len(requests) == 1
    finally:
        assert h.client.portal is not None
        h.client.portal.call(upstream.aclose)
    assert all(h.registry.route_reference_counts((GATEWAY, value)) == (0, 0) for value in h.request.workspace_ids)
