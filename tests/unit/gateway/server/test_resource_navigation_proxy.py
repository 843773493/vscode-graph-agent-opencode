"""导航 canonical 路径必须选定真实 Workspace，而不能漂移到 active target。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.federation.errors import FederationError
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server.resource_navigation import _qualify_result
from app.gateway.server.resource_navigation import router as navigation_router
from app.gateway.server.workspace_proxy import router, verify_user_access_for_proxy
from app.schemas.internal_v2.resource_navigation import ResourceNavigationResultDTO

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
OTHER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


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
async def navigation_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, navigation_relay: _NavigationRelay,
) -> AsyncIterator[tuple[httpx.AsyncClient, GatewayWorkspaceRegistry, list[httpx.Request]]]:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway-root"))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "targets.json")
    for target_id, backend_id, host in (
        ("original-target", WORKSPACE_ID, "original.test"),
        ("active-target", OTHER_WORKSPACE_ID, "active.test"),
    ):
        registry.upsert(WorkspaceTarget(
            workspace_id=target_id, backend_workspace_id=backend_id,
            name=target_id, root_path=str(tmp_path / target_id),
            backend_url=f"http://{host}", connection_kind="local",
        ), activate=True)
    forwarded: list[httpx.Request] = []

    async def receive(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200, json={"data": {"selected": request.url.host}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(receive), trust_env=False) as upstream:
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.include_router(navigation_router)
        application.include_router(router)
        application.state.gateway_id = "gateway_3097a8378d6e2c98a52a687e4a004769"
        application.state.federation_rpc_service = navigation_relay
        application.state.registry = registry
        application.state.http_client = upstream
        application.state.streaming_http_client = upstream
        application.dependency_overrides[verify_user_access_for_proxy] = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://gateway.test",
                                     headers={"X-Local-Token": get_gateway_local_token()}) as client:
            yield client, registry, forwarded


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
    assert registry.active_workspace_id == "active-target"


@pytest.mark.parametrize("explicit_authority", [False, True])
async def test_canonical_remote_navigation_uses_read_grant_instead_of_http_proxy(
    navigation_proxy, navigation_relay: _NavigationRelay, explicit_authority: bool,
) -> None:
    client, registry, forwarded = navigation_proxy
    remote_gateway_id = "gateway_" + "c" * 32
    registry.upsert_remote_gateway(RemoteGatewayConnection(
        connection_id="remote-connection", name="remote", host="remote.test", port=22, username="user",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=remote_gateway_id, protocol_version=1, source_owner="manual",
    ))
    remote_workspace_id = "00000000-0000-4000-8000-000000000003"
    registry.upsert(WorkspaceTarget(
        workspace_id="remote-target", backend_workspace_id=remote_workspace_id, name="remote", root_path=None,
        backend_url="http://remote.test", connection_kind="remote_gateway",
        remote_gateway_connection_id="remote-connection", remote_workspace_id="remote-route",
    ), activate=False)
    uri = f"boxteam://workspace/{remote_workspace_id}/resources/files/root"
    navigation_relay.response = {"node": {"uri": uri, "resource_shape": "directory",
                                          "operations": ["describe", "list"], "child_path": "file_segments"}}
    qualified_uri = uri.replace("boxteam://", f"boxteam://{remote_gateway_id}/")
    response = await client.post(f"/api/v1/workspaces/{remote_workspace_id}/resources/navigation", json={
        "uri": qualified_uri if explicit_authority else uri, "operation": "list",
    }, headers={"X-BoxTeam-Workspace-Id": "active-target"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["node"]["uri"] == qualified_uri
    call, = navigation_relay.calls
    assert call["target"].to_payload() == {"gateway_id": remote_gateway_id, "uri": qualified_uri}
    assert call["body"]["navigation"]["uri"] == qualified_uri
    assert forwarded == [] and registry.active_workspace_id == "active-target"


async def test_navigation_path_selects_registered_target_without_selection_header(navigation_proxy) -> None:
    client, registry, forwarded = navigation_proxy
    assert registry.active_workspace_id == "active-target"
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
    assert registry.route_reference_counts("original-target") == (0, 0)


@pytest.mark.parametrize("mapping", ["unknown", "duplicate", "local_remote_duplicate"])
@pytest.mark.parametrize("suffix", ["resources/navigation", "runtime/log-snapshot/snapshot_id/html"])
async def test_navigation_missing_or_ambiguous_target_never_uses_active(navigation_proxy, mapping: str, suffix: str) -> None:
    client, registry, forwarded = navigation_proxy
    requested_id = "00000000-0000-4000-8000-000000000099"
    if mapping == "duplicate":
        requested_id = WORKSPACE_ID
        registry.upsert(WorkspaceTarget(
            workspace_id="duplicate-target", backend_workspace_id=WORKSPACE_ID,
            name="duplicate", root_path="/unused-duplicate", backend_url="http://duplicate.test",
            connection_kind="local",
        ), activate=False)
    elif mapping == "local_remote_duplicate":
        requested_id = WORKSPACE_ID
        registry.upsert(WorkspaceTarget(
            workspace_id="remote-target", backend_workspace_id=requested_id,
            name="remote", root_path=None, backend_url="http://remote.test",
            connection_kind="remote_gateway", remote_gateway_connection_id="remote-connection",
            remote_workspace_id="remote-route",
        ), activate=False)
    response = await client.post(
        f"/api/v1/workspaces/{requested_id}/{suffix}",
        json={"uri": f"boxteam://workspace/{requested_id}/resources/files/root", "operation": "list"},
    )
    assert response.status_code == (404 if mapping == "unknown" else 409), response.text
    assert forwarded == []
    assert registry.active_workspace_id == "active-target"


@pytest.mark.parametrize("suffix", ["runtime/status", "runtime/log-snapshot/snapshot_id/html", "runtime/log-snapshot/snapshot_id/metadata"])
async def test_canonical_snapshot_and_runtime_path_keep_workspace_after_active_switch(navigation_proxy, suffix: str) -> None:
    client, registry, forwarded = navigation_proxy
    assert registry.active_workspace_id == "active-target"
    response = await client.get(
        f"/api/v1/workspaces/{WORKSPACE_ID}/{suffix}",
        headers={"X-BoxTeam-Workspace-Id": "active-target"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["selected"] == "original.test"
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert forwarded[0].url.path == f"/api/v1/workspaces/{WORKSPACE_ID}/{suffix}"
    assert registry.active_workspace_id == "active-target"


async def test_other_workspace_routes_keep_existing_active_selection(navigation_proxy) -> None:
    client, _, forwarded = navigation_proxy
    response = await client.get("/api/v1/workspace")
    assert response.status_code == 200
    assert response.json()["data"]["selected"] == "active.test"
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == OTHER_WORKSPACE_ID


async def test_navigation_uri_selects_workspace_before_active_or_headers(navigation_proxy) -> None:
    client, registry, forwarded = navigation_proxy
    uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"}, headers={"X-BoxTeam-Workspace-Id": "active-target"})
    assert response.status_code == 200, response.text
    assert forwarded[0].url.host == "original.test"
    assert forwarded[0].url.path == "/api/v1/resources/navigation"
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert registry.active_workspace_id == "active-target"


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
    "/private/host-root/secret",
    "boxteam://workspace/root/resources/files/root/%2Fprivate%2Fhost-root",
])
def test_navigation_remote_response_cannot_change_owner_or_escape_root(bad_uri: str) -> None:
    root = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/src"
    result = ResourceNavigationResultDTO.model_validate({
        "node": {"uri": root, "resource_shape": "directory", "operations": ["describe", "list"], "child_path": "file_segments"},
        "entries": [{"uri": bad_uri, "resource_shape": "file", "operations": ["read"], "child_path": None}],
    })
    with pytest.raises(FederationError) as error:
        _qualify_result(result, uri=root, gateway_id="gateway_target")
    assert error.value.code == "federation-malformed-frame"


@pytest.mark.parametrize("bad", ["uri", "shape", "extra"])
async def test_navigation_invalid_remote_frame_is_sanitized_at_http_boundary(navigation_proxy, navigation_relay: _NavigationRelay, bad: str) -> None:
    client, _, _ = navigation_proxy
    uri = f"boxteam://gateway_target/workspace/{WORKSPACE_ID}/resources/files/root"
    node = {"uri": uri, "resource_shape": "directory", "operations": ["describe", "list"], "child_path": "file_segments"}
    data = {"node": node}
    if bad == "uri":
        node["uri"] = "file:///private/target-secret"
    elif bad == "shape":
        node["resource_shape"] = "/private/target-secret"
    else:
        data["locator"] = "/private/target-secret"
    navigation_relay.response = data
    response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "federation-malformed-frame"
    assert "/private/target-secret" not in response.text


async def test_navigation_policy_denial_and_missing_resource_are_indistinguishable(navigation_proxy, navigation_relay: _NavigationRelay) -> None:
    client, _, _ = navigation_proxy
    uri = f"boxteam://gateway_target/workspace/{WORKSPACE_ID}/resources/files/root"
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
