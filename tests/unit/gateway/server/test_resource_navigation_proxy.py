"""导航 canonical 路径必须选定真实 Workspace，而不能漂移到 active target。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.path_utils import get_gateway_root
from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.federation.errors import FederationError
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceIdentityConflictError,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.resource_navigation import router as navigation_router
from app.gateway.server.workspace_proxy import router, verify_user_access_for_proxy
from app.schemas.internal_v2.resource_navigation import ResourceNavigationResultDTO
from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
    qualify_navigation_result,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    NAVIGATION_DEADLINE_HEADER,
)

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
OTHER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"
REMOTE_GATEWAY_ID = "gateway_" + "c" * 32


@dataclass
class _NavigationRelay:
    response: dict[str, object] | None = None
    error: FederationError | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    async def relay_operation(self, **arguments) -> dict[str, object]:
        assert arguments["operation"] == "read"
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.fixture
def navigation_relay() -> _NavigationRelay:
    return _NavigationRelay()


@pytest.fixture
def proxy_body():
    return {}


@pytest.fixture
async def navigation_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, navigation_relay: _NavigationRelay, proxy_body,
) -> AsyncIterator[tuple[httpx.AsyncClient, GatewayWorkspaceRegistry, list[httpx.Request]]]:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway-root"))
    registry = GatewayWorkspaceRegistry(storage_path=get_gateway_root() / "targets.json")
    for workspace_id, host in (
        (WORKSPACE_ID, "original.test"),
        (OTHER_WORKSPACE_ID, "active.test"),
    ):
        registry.upsert(WorkspaceTarget(
            gateway_id=registry.gateway_id, workspace_id=workspace_id,
            name=host, root_path=str(tmp_path / workspace_id),
            backend_url=f"http://{host}", connection_kind="local",
        ), activate=True)
    forwarded: list[httpx.Request] = []

    async def receive(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        if proxy_body.get("stream") is not None:
            return httpx.Response(200, stream=proxy_body["stream"], headers={"content-type": "application/json"})
        return httpx.Response(200, json={"data": {"selected": request.url.host}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive), trust_env=False) as upstream:
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.include_router(navigation_router)
        application.include_router(router)
        application.state.gateway_id = registry.gateway_id
        application.state.federation_rpc_service = navigation_relay
        application.state.registry = registry
        application.state.http_client = upstream
        application.state.streaming_http_client = upstream
        application.dependency_overrides[verify_user_access_for_proxy] = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://gateway.test",
                                     headers={"X-Local-Token": get_gateway_local_token()}) as client:
            yield client, registry, forwarded
    registry.close()


@pytest.fixture
def publish_remote_workspace(navigation_proxy):
    _, registry, _ = navigation_proxy

    def publish(workspace_id: str = WORKSPACE_ID, *, activate: bool = False) -> WorkspaceTarget:
        connection = RemoteGatewayConnection(
            connection_id="remote-connection", name="remote", host="remote.test", port=22, username="user",
            private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
            remote_gateway_id=REMOTE_GATEWAY_ID, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        )
        target = WorkspaceTarget(
            gateway_id=REMOTE_GATEWAY_ID, workspace_id=workspace_id,
            name="remote", root_path=None, backend_url="http://remote.test", connection_kind="remote_gateway",
            owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,),
            remote_service_names=("workspace_api",),
        )
        registry.apply_remote_projection_snapshot(
            connection=connection, projections=(target,), activate=activate,
            runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}),
        )
        return registry.resolve(target.key)

    return publish


@pytest.mark.parametrize("authority,scope,status", [
    (None, "local", 200), ("local", "local", 200),
    ("remote", "remote", 200),
    (None, "remote", 403), ("remote", "local", 403),
])
async def test_shared_backend_gateway_scope_keeps_explicit_owner(
    navigation_proxy, navigation_relay, publish_remote_workspace, authority, scope, status,
):
    client, registry, forwarded = navigation_proxy
    publish_remote_workspace()
    owners = {"local": registry.gateway_id, "remote": REMOTE_GATEWAY_ID}
    authority = owners[authority] if authority is not None else None
    scope_id = owners[scope]
    local_uri = f"boxteam://gateway/{scope_id}/resources/skills/test/SKILL.md"
    uri = local_uri if authority is None else local_uri.replace("boxteam://", f"boxteam://{authority}/")
    navigation_relay.response = {"node": {"uri": local_uri, "resource_shape": "file",
        "operations": ["describe", "read"]}, "content": "REMOTE SKILL"}
    response = await client.post(f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation",
        json={"uri": uri, "operation": "read"}, headers={"X-BoxTeam-Workspace-Id": WORKSPACE_ID})
    assert response.status_code == status, response.text
    if status != 200:
        assert response.json()["detail"]["code"] == "scope_mismatch"
        assert not forwarded and not navigation_relay.calls
    elif scope_id == registry.gateway_id:
        outgoing, = forwarded
        assert outgoing.url.host == "original.test"
        assert outgoing.url.path == f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation"
        assert outgoing.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
        assert json.loads(outgoing.content)["uri"] == local_uri
        assert not navigation_relay.calls
        assert registry.route_reference_counts(WORKSPACE_ID) == (0, 0)
    else:
        call, = navigation_relay.calls
        assert call["target"].to_payload() == {"gateway_id": REMOTE_GATEWAY_ID, "uri": uri}
        assert response.json()["data"]["content"] == "REMOTE SKILL"
        assert response.json()["data"]["node"]["uri"] == uri
        assert not forwarded
    assert registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)


async def test_shared_backend_workspace_authority_routes_only_to_remote_owner(
    navigation_proxy, navigation_relay, publish_remote_workspace,
):
    client, registry, forwarded = navigation_proxy
    publish_remote_workspace()
    local_uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    uri = local_uri.replace("boxteam://", f"boxteam://{REMOTE_GATEWAY_ID}/")
    navigation_relay.response = {"node": {"uri": local_uri, "resource_shape": "directory",
        "operations": ["describe", "list"], "child_path": "file_segments"}}
    response = await client.post(f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation",
        json={"uri": uri, "operation": "list"})
    assert response.status_code == 200, response.text
    call, = navigation_relay.calls
    assert call["target"].to_payload() == {"gateway_id": REMOTE_GATEWAY_ID, "uri": uri}
    assert response.json()["data"]["node"]["uri"] == uri
    assert not forwarded and registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)


async def test_conflicting_root_registration_preserves_original_navigation(navigation_proxy, navigation_relay):
    client, registry, forwarded = navigation_proxy
    original = registry.resolve(WORKSPACE_ID)
    before = registry._capture_registry_state()
    persisted = registry._storage_path.read_bytes()
    assert original.root_path is not None
    candidate = replace(original, root_path=str(Path(original.root_path).with_name("duplicate")), target_generation="")
    with pytest.raises(WorkspaceIdentityConflictError, match="UUID 已绑定另一物理根"):
        registry.upsert(candidate, activate=False)
    assert candidate.target_generation == ""
    assert registry._capture_registry_state() == before
    assert registry._storage_path.read_bytes() == persisted
    response = await client.post(f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation", json={
        "uri": f"boxteam://gateway/{registry.gateway_id}/resources/skills/test/SKILL.md", "operation": "read"})
    assert response.status_code == 200, response.text
    outgoing, = forwarded
    assert outgoing.url.host == "original.test"
    assert not navigation_relay.calls
    assert registry.active_workspace == before["active_workspace"]
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


@pytest.mark.parametrize("deadline_at,reason", [("nan", "invalid_deadline"), ("inf", "invalid_deadline"),
    ("garbage", "invalid_deadline"), ("1", "deadline")])
async def test_invalid_or_expired_execution_deadline_never_routes(navigation_proxy, navigation_relay, deadline_at, reason):
    client, _, forwarded = navigation_proxy
    uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"},
        headers={NAVIGATION_DEADLINE_HEADER: deadline_at})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["reason"] == reason
    assert not forwarded and not navigation_relay.calls


@pytest.mark.parametrize("remote", [False, True])
async def test_execution_header_caps_route_without_changing_query(navigation_proxy, navigation_relay, remote):
    client, _, forwarded = navigation_proxy
    local_uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    uri = local_uri.replace("boxteam://", f"boxteam://{REMOTE_GATEWAY_ID}/") if remote else local_uri
    navigation_relay.response = {"node": {"uri": local_uri, "resource_shape": "directory",
        "operations": ["describe", "list"], "child_path": "file_segments"}}
    cap = time.time() + 0.5
    query = {"uri": uri, "operation": "list", "timeout_ms": 1234}
    response = await client.post("/api/v1/resources/navigation", json=query, headers={NAVIGATION_DEADLINE_HEADER: str(cap)})
    assert response.status_code == 200, response.text
    if remote:
        call, = navigation_relay.calls
        assert abs(call["deadline_at"] - cap) < 0.001
        assert call["body"]["navigation"]["timeout_ms"] == 1234
        assert not forwarded
    else:
        outgoing, = forwarded
        assert abs(float(outgoing.headers[NAVIGATION_DEADLINE_HEADER]) - cap) < 0.001
        assert json.loads(outgoing.content) == query
        assert not navigation_relay.calls


async def test_proxy_consumes_navigation_body_within_deadline_and_releases_route(navigation_proxy, proxy_body):
    client, registry, _ = navigation_proxy

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = proxy_body["stream"] = Stream()
    started = time.monotonic()
    response = await client.post("/api/v1/resources/navigation", json={
        "uri": f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root", "operation": "list", "timeout_ms": 80})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["reason"] == "deadline"
    assert time.monotonic() - started < 0.8
    assert stream.closed and registry.route_reference_counts(WORKSPACE_ID) == (0, 0)


@pytest.fixture
def federation_headers(navigation_proxy) -> dict[str, str]:
    credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
        connection_id="peer", peer_gateway_id="gateway_" + "b" * 32,
    )
    return {"X-Local-Token": "", "X-BoxTeam-Federation-Token": credential.token}


@pytest.mark.parametrize("canonical", [False, True])
async def test_federation_http_navigation_requires_signed_read_grant(
    navigation_proxy, navigation_relay: _NavigationRelay, federation_headers: dict[str, str], canonical: bool,
) -> None:
    client, registry, forwarded = navigation_proxy
    path = f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation" if canonical else "/api/v1/resources/navigation"
    response = await client.post(path, headers=federation_headers, json={
        "uri": f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root", "operation": "list",
    })
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "联邦资源导航必须通过原 read grant relay"
    assert forwarded == [] and navigation_relay.calls == []
    assert registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)


@pytest.mark.parametrize("access", ["registered", "unregistered", "wrong-token", "federation-token"])
async def test_workspace_service_navigation_requires_local_credential_and_registered_source(
    navigation_proxy, navigation_relay: _NavigationRelay, federation_headers, access: str,
) -> None:
    client, registry, forwarded = navigation_proxy
    remote_gateway = "gateway_" + "c" * 32
    local_uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    navigation_relay.response = {"node": {"uri": local_uri, "resource_shape": "directory",
        "operations": ["describe", "list"], "child_path": "file_segments"}}
    uri = local_uri.replace("boxteam://", f"boxteam://{remote_gateway}/")
    source = "00000000-0000-4000-8000-000000000099" if access == "unregistered" else WORKSPACE_ID
    headers = {"X-Local-Token": "wrong"} if access == "wrong-token" else federation_headers if access == "federation-token" else {}
    response = await client.post(f"/api/gateway/resource-navigation/workspaces/{source}",
        json={"uri": uri, "operation": "list"}, headers=headers)
    if access == "registered":
        assert response.status_code == 200, response.text
        assert response.json()["data"]["node"]["uri"] == uri and len(navigation_relay.calls) == 1
    else:
        assert response.status_code == (403 if access == "unregistered" else 401), response.text
        assert navigation_relay.calls == []
    assert forwarded == [] and registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)


@pytest.mark.parametrize("explicit_authority", [False, True])
async def test_canonical_remote_navigation_uses_read_grant_instead_of_http_proxy(
    navigation_proxy, navigation_relay: _NavigationRelay, publish_remote_workspace, explicit_authority: bool,
) -> None:
    client, registry, forwarded = navigation_proxy
    workspace_id = "00000000-0000-4000-8000-000000000003"
    remote = publish_remote_workspace(workspace_id)
    before = registry._capture_registry_state()
    uri = f"boxteam://workspace/{workspace_id}/resources/files/root"
    navigation_relay.response = {"node": {"uri": uri, "resource_shape": "directory",
                                          "operations": ["describe", "list"], "child_path": "file_segments"}}
    qualified_uri = uri.replace("boxteam://", f"boxteam://{REMOTE_GATEWAY_ID}/")
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-Request-ID": "request-remote-navigation"}
    if not explicit_authority:
        headers[TARGET_GATEWAY_ID_HEADER] = REMOTE_GATEWAY_ID
    started = time.time()
    response = await client.post(f"/api/v1/workspaces/{workspace_id}/resources/navigation", json={
        "uri": qualified_uri if explicit_authority else uri, "operation": "list", "timeout_ms": 1234,
    }, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["node"]["uri"] == qualified_uri
    call, = navigation_relay.calls
    assert call["target"].to_payload() == {"gateway_id": REMOTE_GATEWAY_ID, "uri": qualified_uri}
    assert call["body"]["navigation"]["uri"] == qualified_uri
    assert started < call["deadline_at"] <= time.time() + 1.234
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "request-remote-navigation"
    assert forwarded == [] and registry._capture_registry_state() == before
    assert registry.route_reference_counts(remote.key) == (0, 0)


async def test_navigation_path_selects_registered_target_without_selection_header(navigation_proxy) -> None:
    client, registry, forwarded = navigation_proxy
    assert registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)
    uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    response = await client.post(
        f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation",
        json={"uri": uri, "operation": "list"}, headers={"X-Request-ID": "request-fixed-navigation"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["selected"] == "original.test"
    assert len(forwarded) == 1
    request = forwarded[0]
    assert request.url.path == f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation"
    assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert request.headers["X-Request-ID"] == response.headers["X-Request-ID"] == "request-fixed-navigation"
    assert json.loads(request.content) == {"uri": uri, "operation": "list"}
    assert registry.route_reference_counts(WORKSPACE_ID) == (0, 0)


async def test_native_directory_navigation_selects_same_machine_independent_of_active(navigation_proxy) -> None:
    client, registry, forwarded = navigation_proxy
    before = registry._capture_registry_state()
    uri = f"boxteam://gateway/{registry.gateway_id}/resources/files/01900000000070008000000000000001"
    response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"},
                                 headers={"X-BoxTeam-Workspace-Id": OTHER_WORKSPACE_ID})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["selected"] == "original.test"
    assert len(forwarded) == 1
    assert json.loads(forwarded[0].content)["uri"] == uri
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)
    assert registry._capture_registry_state() == before
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


@pytest.mark.parametrize("suffix", ["resources/navigation", "runtime/log-snapshot/snapshot_id/html"])
async def test_navigation_unknown_target_never_uses_active(navigation_proxy, navigation_relay, suffix: str) -> None:
    client, registry, forwarded = navigation_proxy
    requested_id = "00000000-0000-4000-8000-000000000099"
    before = registry._capture_registry_state()
    response = await client.post(
        f"/api/v1/workspaces/{requested_id}/{suffix}",
        json={"uri": f"boxteam://workspace/{requested_id}/resources/files/root", "operation": "list"},
    )
    assert response.status_code == 404, response.text
    assert forwarded == navigation_relay.calls == []
    assert registry._capture_registry_state() == before
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


@pytest.mark.parametrize("suffix", ["resources/navigation", "runtime/log-snapshot/snapshot_id/html"])
async def test_same_uuid_remote_owner_does_not_replace_native_navigation(
    navigation_proxy, navigation_relay, publish_remote_workspace, suffix: str,
) -> None:
    client, registry, forwarded = navigation_proxy
    remote = publish_remote_workspace(activate=True)
    before = registry._capture_registry_state()
    response = await client.post(f"/api/v1/workspaces/{WORKSPACE_ID}/{suffix}", json={
        "uri": f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root", "operation": "list",
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["selected"] == "original.test"
    outgoing, = forwarded
    assert outgoing.url.host == "original.test"
    assert outgoing.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert TARGET_GATEWAY_ID_HEADER not in outgoing.headers
    assert navigation_relay.calls == []
    assert registry.active_workspace == remote.key
    assert registry._capture_registry_state() == before
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


@pytest.mark.parametrize("suffix", ["runtime/status", "runtime/log-snapshot/snapshot_id/html", "runtime/log-snapshot/snapshot_id/metadata"])
async def test_canonical_snapshot_and_runtime_path_keep_workspace_after_active_switch(navigation_proxy, suffix: str) -> None:
    client, registry, forwarded = navigation_proxy
    assert registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)
    response = await client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/{suffix}",
        headers={"X-BoxTeam-Workspace-Id": WORKSPACE_ID},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["selected"] == "original.test"
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert forwarded[0].url.path == f"/api/v1/workspaces/{WORKSPACE_ID}/{suffix}"
    assert registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)


async def test_other_workspace_routes_keep_existing_active_selection(navigation_proxy) -> None:
    client, _, forwarded = navigation_proxy
    response = await client.get("/api/v1/workspace")
    assert response.status_code == 200
    assert response.json()["data"]["selected"] == "active.test"
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == OTHER_WORKSPACE_ID


async def test_navigation_uri_selects_workspace_before_active_or_headers(navigation_proxy, navigation_relay) -> None:
    client, registry, forwarded = navigation_proxy
    before = registry._capture_registry_state()
    uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"},
        headers={"X-BoxTeam-Workspace-Id": OTHER_WORKSPACE_ID, "X-Request-ID": "request-uri-before-context"})
    assert response.status_code == 200, response.text
    outgoing, = forwarded
    assert outgoing.url.host == "original.test"
    assert outgoing.url.path == "/api/v1/resources/navigation"
    assert outgoing.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert outgoing.headers["X-Request-ID"] == response.headers["X-Request-ID"] == "request-uri-before-context"
    assert json.loads(outgoing.content)["uri"] == uri
    assert navigation_relay.calls == []
    assert registry.active_workspace == (registry.gateway_id, OTHER_WORKSPACE_ID)
    assert registry._capture_registry_state() == before
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


async def test_navigation_workspace_header_conflict_never_admits_route(
    navigation_proxy, navigation_relay,
) -> None:
    client, registry, forwarded = navigation_proxy
    before = registry._capture_registry_state()
    path = f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation"
    response = await client.post(path, json={
        "uri": f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root", "operation": "list",
    }, headers={"X-BoxTeam-Workspace-Id": OTHER_WORKSPACE_ID})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "路径与请求头的工作区 UUID 不一致"
    assert forwarded == navigation_relay.calls == []
    assert registry._capture_registry_state() == before
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


@pytest.mark.parametrize("canonical", [False, True])
async def test_navigation_invalid_gateway_authority_is_rejected_before_owner_lookup(
    navigation_proxy, navigation_relay, canonical: bool,
) -> None:
    client, registry, forwarded = navigation_proxy
    before = registry._capture_registry_state()
    uri = f"boxteam://gateway_other/workspace/{WORKSPACE_ID}/resources/files/root"
    assert parse_resource_uri(uri).gateway_authority == "gateway_other"
    request_id = f"request-invalid-navigation-owner-{canonical}"
    path = f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation" if canonical else "/api/v1/resources/navigation"
    response = await client.post(path, json={"uri": uri, "operation": "list"},
                                 headers={"X-Request-ID": request_id})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "非法 gateway_id（要求 gateway_<32位小写hex>）: 'gateway_other'"
    assert response.headers["X-Request-ID"] == request_id
    assert forwarded == navigation_relay.calls == []
    assert registry._capture_registry_state() == before
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


async def test_navigation_unregistered_gateway_authority_keeps_not_found(
    navigation_proxy, navigation_relay,
) -> None:
    client, registry, forwarded = navigation_proxy
    before = registry._capture_registry_state()
    assert not registry.has_target((REMOTE_GATEWAY_ID, WORKSPACE_ID))
    uri = f"boxteam://{REMOTE_GATEWAY_ID}/workspace/{WORKSPACE_ID}/resources/files/root"
    response = await client.post(f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation",
        json={"uri": uri, "operation": "list"}, headers={"X-Request-ID": "request-unknown-navigation-owner"})
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "workspace 未注册或不存在"
    assert response.headers["X-Request-ID"] == "request-unknown-navigation-owner"
    assert forwarded == navigation_relay.calls == []
    assert registry._capture_registry_state() == before
    assert all(registry.route_reference_counts(target.key) == (0, 0) for target in registry.targets())


async def test_navigation_canonical_scope_mismatch_never_reaches_backend(navigation_proxy) -> None:
    client, _, forwarded = navigation_proxy
    response = await client.post(f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation", json={
        "uri": f"boxteam://workspace/{OTHER_WORKSPACE_ID}/resources/files/root", "operation": "list",
    })
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "scope_mismatch"
    assert forwarded == []


@pytest.mark.parametrize("bad_uri", [
    f"boxteam://workspace/{OTHER_WORKSPACE_ID}/resources/files/root/src/child.txt",
    f"boxteam://gateway_other/workspace/{WORKSPACE_ID}/resources/files/root/src/child.txt",
    f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/outside.txt",
    f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/src",
])
def test_navigation_remote_response_cannot_change_owner_or_escape_root(bad_uri: str) -> None:
    root = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/src"
    result = ResourceNavigationResultDTO.model_validate({
        "node": {"uri": root, "resource_shape": "directory", "operations": ["describe", "list"], "child_path": "file_segments"},
        "entries": [{"uri": bad_uri, "resource_shape": "file", "operations": ["read"], "child_path": None}],
    })
    with pytest.raises(FederationError) as error:
        qualify_navigation_result(result, uri=root, gateway_id="gateway_target")
    assert error.value.code == "federation-malformed-frame"


def test_search_match_uris_are_qualified_and_must_belong_to_returned_files() -> None:
    uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/src/file.txt"
    result = ResourceNavigationResultDTO.model_validate({
        "node": {"uri": uri, "resource_shape": "file", "operations": ["describe", "read", "search"]},
        "entries": [{"uri": uri, "resource_shape": "file", "operations": ["read", "search"]}],
        "matches": [{"uri": uri, "line": 3, "column": 7, "text": "needle", "snippet_start_column": 7}],
        "contexts": [{"uri": uri, "line": 2, "text": "before"}],
    })
    qualified = qualify_navigation_result(result, uri=uri, gateway_id="gateway_target")
    expected = uri.replace("boxteam://", "boxteam://gateway_target/")
    assert qualified.node.uri == qualified.entries[0].uri == qualified.matches[0].uri == qualified.contexts[0].uri == expected
    for member in ("matches", "contexts"):
        bad = getattr(result, member)[0].model_copy(update={"uri": uri.replace("file.txt", "unreturned.txt")})
        with pytest.raises(FederationError) as failure:
            qualify_navigation_result(result.model_copy(update={member: (bad,)}), uri=uri, gateway_id="gateway_target")
        assert failure.value.code == "federation-malformed-frame"


@pytest.mark.parametrize("bad", ["uri", "entry", "match", "windows", "unc", "shape", "extra"])
async def test_navigation_invalid_remote_frame_is_sanitized_at_http_boundary(navigation_proxy, navigation_relay: _NavigationRelay, bad: str) -> None:
    client, _, _ = navigation_proxy
    uri = f"boxteam://{REMOTE_GATEWAY_ID}/workspace/{WORKSPACE_ID}/resources/files/root"
    node = {"uri": uri, "resource_shape": "directory", "operations": ["describe", "list"], "child_path": "file_segments"}
    data = {"node": node}
    if bad == "uri":
        node["uri"] = "file:///private/target-secret"
    elif bad == "entry":
        data["entries"] = [{**node, "uri": "/private/target-secret"}]
    elif bad == "match":
        data["matches"] = [{"uri": "/private/target-secret", "line": 1, "column": 1,
                            "text": "正文不限制路径", "snippet_start_column": 1}]
    elif bad == "windows":
        node["uri"] = r"C:\private\target-secret"
    elif bad == "unc":
        node["uri"] = r"\\host\private\target-secret"
    elif bad == "shape":
        node["resource_shape"] = "/private/target-secret"
    else:
        data["locator"] = "/private/target-secret"
    navigation_relay.response = data
    response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "federation-malformed-frame"
    assert "/private/target-secret" not in response.text
    assert "target-secret" not in response.text


async def test_navigation_policy_denial_and_missing_resource_are_indistinguishable(navigation_proxy, navigation_relay: _NavigationRelay) -> None:
    client, _, _ = navigation_proxy
    uri = f"boxteam://{REMOTE_GATEWAY_ID}/workspace/{WORKSPACE_ID}/resources/files/root"
    details = []
    for error in (
        FederationError("authorization-revoked", "策略不允许访问", detail={"capability": "read", "policy_revision": "rev-123"}),
        FederationError("target_not_resolvable", "不存在的目标"),
    ):
        navigation_relay.error = error
        response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"})
        assert response.status_code == 409
        details.append(response.json()["detail"])
    assert details[0] == details[1] == {"code": "target_not_resolvable", "message": "导航资源未共享或不存在", "detail": {}}
