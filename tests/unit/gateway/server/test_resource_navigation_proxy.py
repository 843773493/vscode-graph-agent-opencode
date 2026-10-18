"""导航 canonical 路径必须选定真实 Workspace，而不能漂移到 active target。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server.workspace_proxy import router, verify_user_access_for_proxy

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
OTHER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
async def navigation_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        application.include_router(router)
        application.state.registry = registry
        application.state.http_client = upstream
        application.state.streaming_http_client = upstream
        application.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
        application.dependency_overrides[verify_user_access_for_proxy] = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://gateway.test") as client:
            yield client, registry, forwarded


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


@pytest.mark.parametrize("mapping", ["unknown", "duplicate", "remote_only"])
async def test_navigation_missing_or_ambiguous_local_target_never_uses_active(navigation_proxy, mapping: str) -> None:
    client, registry, forwarded = navigation_proxy
    requested_id = "00000000-0000-4000-8000-000000000099"
    if mapping == "duplicate":
        requested_id = WORKSPACE_ID
        registry.upsert(WorkspaceTarget(
            workspace_id="duplicate-target", backend_workspace_id=WORKSPACE_ID,
            name="duplicate", root_path="/unused-duplicate", backend_url="http://duplicate.test",
            connection_kind="local",
        ), activate=False)
    elif mapping == "remote_only":
        registry.upsert(WorkspaceTarget(
            workspace_id="remote-target", backend_workspace_id=requested_id,
            name="remote", root_path="/unused-remote", backend_url="http://remote.test",
            connection_kind="remote_gateway", remote_gateway_connection_id="remote-connection",
            remote_workspace_id="remote-route",
        ), activate=False)
    response = await client.post(
        f"/api/v1/workspaces/{requested_id}/resources/navigation",
        json={"uri": f"boxteam://workspace/{requested_id}/resources/files/root", "operation": "list"},
    )
    assert response.status_code == (409 if mapping == "duplicate" else 404), response.text
    assert forwarded == []
    assert registry.active_workspace_id == "active-target"


async def test_other_workspace_routes_keep_existing_active_selection(navigation_proxy) -> None:
    client, _, forwarded = navigation_proxy
    response = await client.get("/api/v1/workspace")
    assert response.status_code == 200
    assert response.json()["data"]["selected"] == "active.test"
    assert forwarded[0].headers["X-BoxTeam-Workspace-Id"] == OTHER_WORKSPACE_ID
