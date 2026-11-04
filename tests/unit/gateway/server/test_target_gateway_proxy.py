"""目标 Gateway 归属必须在原工作区代理准入前确定，与发送方身份分离。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.federation.grants import (
    ContextGrantTarget,
    ResourceNavigationGrantTarget,
)
from app.gateway.main import gateway_http_exception_handler
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.resource_navigation import router as navigation_router
from app.gateway.server.workspace_proxy import router, verify_user_access_for_proxy
from app.services.business.session_context_resource import (
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
REMOTE_GATEWAY_IDS = ("gateway_" + "b" * 32, "gateway_" + "c" * 32)


@dataclass
class Relay:
    gateway_id: str
    calls: list[dict[str, object]] = field(default_factory=list)

    async def relay_operation(self, **arguments: object) -> dict[str, object]:
        self.calls.append(arguments)
        target = arguments["target"]
        if isinstance(target, ContextGrantTarget):
            return {"selected_gateway": target.gateway_id, "uri": target.uri}
        assert isinstance(target, ResourceNavigationGrantTarget)
        uri = target.uri
        return {"node": {"uri": uri, "resource_shape": "file", "operations": ["describe", "read"]}, "content": "REMOTE"}


@dataclass
class Proxy:
    client: httpx.AsyncClient
    registry: GatewayWorkspaceRegistry
    gateway_id: str
    requests: list[httpx.Request]
    relay: Relay


@pytest.fixture
async def target_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Proxy]:
    root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    gateway_id = load_or_create_gateway_id(root / "identity.json")
    registry = GatewayWorkspaceRegistry(storage_path=root / "workspaces.json")
    registry.upsert(WorkspaceTarget(
        workspace_id="local-route", backend_workspace_id=WORKSPACE_ID, name="本机",
        root_path=str(tmp_path / "native"), backend_url="http://local.test", connection_kind="local",
    ))
    for index, owner in enumerate(REMOTE_GATEWAY_IDS):
        connection_id = f"rgw_{index}"
        registry.upsert_remote_gateway(RemoteGatewayConnection(
            connection_id=connection_id, name="远端", host=f"remote-{index}.test", port=22, username="unit",
            private_key_path=None, ssh_config_host=None, remote_gateway_port=8014, remote_gateway_id=owner,
            protocol_version=1, source_owner="manual",
        ), runtime=WorkspaceRuntime(service_urls={"workspace_api": f"http://remote-{index}.test"}))
        registry.upsert(WorkspaceTarget(
            workspace_id=f"remote-route-{index}", backend_workspace_id=WORKSPACE_ID, name="远端",
            root_path=None, backend_url=f"http://remote-{index}.test", connection_kind="remote_gateway",
            connection_id=connection_id, remote_gateway_connection_id=connection_id, remote_workspace_id=f"native-route-{index}",
        ), activate=False)
        FederationCredentialStore(storage_path=root / "credentials/federation.json").issue(
            connection_id=connection_id, peer_gateway_id=gateway_id,
        )
    forwarded: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200, json={"data": {"host": request.url.host},
                                         "request_id": request.headers["X-Request-ID"]})

    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    async def http_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, HTTPException)
        return await gateway_http_exception_handler(request, error)

    app.add_exception_handler(HTTPException, http_error)
    app.include_router(navigation_router)
    app.include_router(router)
    app.dependency_overrides[verify_user_access_for_proxy] = lambda: None
    app.state.registry = registry
    app.state.gateway_id = gateway_id
    relay = Relay(gateway_id)
    app.state.federation_rpc_service = relay
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as upstream:
        app.state.http_client = app.state.streaming_http_client = upstream
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test",
                                    headers={"X-Local-Token": get_gateway_local_token()}) as client:
            try:
                yield Proxy(client, registry, gateway_id, forwarded, relay)
            finally:
                registry.close()


@pytest.mark.parametrize("index", [None, 0, 1])
async def test_canonical_workspace_route_uses_target_gateway_without_merging_equal_uuid(target_proxy: Proxy, index: int | None) -> None:
    state = target_proxy
    owner = state.gateway_id if index is None else REMOTE_GATEWAY_IDS[index]
    response = await state.client.get(f"/api/v1/workspaces/{WORKSPACE_ID}/workspace", headers={
        TARGET_GATEWAY_ID_HEADER: owner, "X-BoxTeam-Gateway-Id": "gateway_" + "f" * 32,
        "X-Request-ID": "req_target_owner",
    })
    assert response.status_code == 200, response.text
    outgoing, = state.requests
    assert outgoing.url.host == ("local.test" if index is None else f"remote-{index}.test")
    assert outgoing.headers["X-BoxTeam-Gateway-Id"] == state.gateway_id
    assert outgoing.headers["X-Request-ID"] == response.headers["X-Request-ID"] == "req_target_owner"
    assert outgoing.url.path == f"/api/v1/workspaces/{WORKSPACE_ID}/workspace"
    assert outgoing.headers.get(TARGET_GATEWAY_ID_HEADER) == (None if index is None else owner)
    assert state.registry.active_workspace_id == "local-route"
    assert all(state.registry.route_reference_counts(target.workspace_id) == (0, 0) for target in state.registry.targets())


async def test_equal_uuid_without_explicit_owner_still_reports_ambiguity(target_proxy: Proxy) -> None:
    response = await target_proxy.client.get(f"/api/v1/workspaces/{WORKSPACE_ID}/workspace")
    assert response.status_code == 409, response.text
    assert target_proxy.requests == []


@pytest.mark.parametrize("owner,status", [("", 400), ("http://remote.test", 400), ("gateway_" + "e" * 32, 404)])
async def test_invalid_or_unregistered_target_gateway_never_uses_active_workspace(target_proxy: Proxy, owner: str, status: int) -> None:
    before = target_proxy.registry.registry_revision
    response = await target_proxy.client.get(f"/api/v1/workspaces/{WORKSPACE_ID}/workspace", headers={TARGET_GATEWAY_ID_HEADER: owner})
    assert response.status_code == status, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert target_proxy.requests == []
    assert target_proxy.registry.registry_revision == before


async def test_selected_workspace_and_gateway_conflict_is_rejected_before_proxy_admission(target_proxy: Proxy) -> None:
    response = await target_proxy.client.get("/api/v1/workspace", headers={
        "X-BoxTeam-Workspace-Id": "local-route", TARGET_GATEWAY_ID_HEADER: REMOTE_GATEWAY_IDS[0],
    })
    assert response.status_code == 400, response.text
    assert "归属不一致" in response.json()["detail"]
    assert target_proxy.requests == []
    assert target_proxy.registry.route_reference_counts("local-route") == (0, 0)


@pytest.mark.parametrize("index", [0, 1])
async def test_workspace_navigation_disambiguates_equal_uuid_before_rpc(target_proxy: Proxy, index: int) -> None:
    uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/main.py"
    response = await target_proxy.client.post(f"/api/v1/workspaces/{WORKSPACE_ID}/resources/navigation",
        headers={TARGET_GATEWAY_ID_HEADER: REMOTE_GATEWAY_IDS[index]}, json={"uri": uri, "operation": "read"})
    assert response.status_code == 200, response.text
    call, = target_proxy.relay.calls
    target = call["target"]
    assert isinstance(target, ResourceNavigationGrantTarget)
    assert target.gateway_id == REMOTE_GATEWAY_IDS[index]
    assert json.loads(response.content)["data"]["content"] == "REMOTE"
    assert target_proxy.requests == []


@pytest.mark.parametrize("explicit_uri", [False, True])
async def test_navigation_uri_cannot_be_retargeted_by_gateway_header(target_proxy: Proxy, explicit_uri: bool) -> None:
    uri = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root/main.py"
    if explicit_uri:
        uri = uri.replace("boxteam://", f"boxteam://{target_proxy.gateway_id}/")
    response = await target_proxy.client.post("/api/v1/resources/navigation",
        headers={TARGET_GATEWAY_ID_HEADER: REMOTE_GATEWAY_IDS[0]}, json={"uri": uri, "operation": "read"})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "目标 Gateway 与导航 URI 归属不一致"
    assert target_proxy.requests == target_proxy.relay.calls == []


@pytest.mark.parametrize("operation", ["read", "search"])
@pytest.mark.parametrize("remote", [False, True])
async def test_context_target_gateway_keeps_uri_owner_and_request_identity(
    target_proxy: Proxy, operation: str, remote: bool,
) -> None:
    owner = REMOTE_GATEWAY_IDS[0] if remote else target_proxy.gateway_id
    uri = workspace_session_inventory_uri(WORKSPACE_ID, gateway_authority=owner)
    payload = {"resource_identity": str(workspace_session_inventory_identity(WORKSPACE_ID)),
               "uri": uri, "scope": "workspace"}
    if operation == "search":
        payload["query"] = "目标内容"
    response = await target_proxy.client.post(f"/api/v1/context/{operation}", json=payload,
        headers={TARGET_GATEWAY_ID_HEADER: owner, "X-Request-ID": "req_context_target"})
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req_context_target"
    if remote:
        call, = target_proxy.relay.calls
        target = call["target"]
        assert isinstance(target, ContextGrantTarget)
        assert target.gateway_id == owner and target.uri == uri
        assert call["operation"] == "read"
        assert target_proxy.requests == []
    else:
        outgoing, = target_proxy.requests
        assert outgoing.url.host == "local.test"
        assert outgoing.headers["X-BoxTeam-Gateway-Id"] == owner
        assert TARGET_GATEWAY_ID_HEADER not in outgoing.headers
        assert json.loads(outgoing.content)["uri"] == workspace_session_inventory_uri(WORKSPACE_ID)
        assert target_proxy.relay.calls == []
    assert all(target_proxy.registry.route_reference_counts(target.workspace_id) == (0, 0)
               for target in target_proxy.registry.targets())


@pytest.mark.parametrize("operation", ["read", "search"])
@pytest.mark.parametrize("uri_owner", [None, *REMOTE_GATEWAY_IDS])
async def test_context_uri_cannot_be_retargeted_by_gateway_header(
    target_proxy: Proxy, operation: str, uri_owner: str | None,
) -> None:
    payload = {"resource_identity": str(workspace_session_inventory_identity(WORKSPACE_ID)),
               "uri": workspace_session_inventory_uri(WORKSPACE_ID, gateway_authority=uri_owner),
               "scope": "workspace"}
    if operation == "search":
        payload["query"] = "目标内容"
    wrong_owner = REMOTE_GATEWAY_IDS[0] if uri_owner is None else target_proxy.gateway_id
    response = await target_proxy.client.post(f"/api/v1/context/{operation}", json=payload,
        headers={TARGET_GATEWAY_ID_HEADER: wrong_owner})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "目标 Gateway 与上下文 URI 归属不一致"
    assert target_proxy.requests == target_proxy.relay.calls == []
    assert all(target_proxy.registry.route_reference_counts(target.workspace_id) == (0, 0)
               for target in target_proxy.registry.targets())
