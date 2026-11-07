"""Backend 私有宿主入口必须在创建进程前拒绝非管理通道。"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app.api.extension_host import router
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.trace_middleware import TraceMiddleware


@pytest.fixture
def private_host_client(empty_workspace_scope: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, WorkspaceContainerRegistry]]:
    workspace_id, root = empty_workspace_scope
    monkeypatch.delenv("BOXTEAM_RUNTIME_MANIFEST", raising=False)
    monkeypatch.setenv("BOXTEAM_HOME", str(root.parent / "boxteam-home"))
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", "private-host-token")
    registry = WorkspaceContainerRegistry(mounted_workspaces=MountedWorkspaceRegistry([
        MountedWorkspace(workspace_id, root)]), backend_id=str(uuid4()))
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router)
    application.state.workspace_container_registry = registry
    try:
        with TestClient(application, client=("127.0.0.1", 42000)) as client:
            yield client, registry
    finally:
        registry.close()


@pytest.mark.parametrize(("headers", "status"), [
    ({}, 401),
    ({"X-Local-Token": "local-dev-token"}, 401),
    ({"X-Local-Token": "private-host-token", "Origin": "https://browser.invalid"}, 403),
    ({"X-Local-Token": "private-host-token", "Sec-Fetch-Site": "same-origin"}, 403),
    ({"X-Local-Token": "private-host-token"}, 400),
])
def test_backend_rejects_untrusted_channel_before_host_creation(
    private_host_client: tuple[TestClient, WorkspaceContainerRegistry], headers: dict[str, str], status: int,
) -> None:
    client, registry = private_host_client
    with pytest.raises(WebSocketDenialResponse) as rejected, client.websocket_connect(
        "/api/internal/extension-hosts/channel", headers=headers,
    ):
        pass
    assert rejected.value.status_code == status
    assert registry.extension_hosts.active == ()


def test_private_discovery_reports_real_identity_and_never_starts_host(
    private_host_client: tuple[TestClient, WorkspaceContainerRegistry],
) -> None:
    client, registry = private_host_client
    owner = registry.extension_hosts
    payload = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32,
               "workspace_ids": list(registry.mounted_workspaces.workspace_ids)}
    headers = {"X-Local-Token": "private-host-token", "X-BoxTeam-Gateway-Id": payload["gateway_id"],
               "X-Request-ID": "discover-private"}
    response = client.post("/api/internal/extension-hosts/resolve", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "discover-private"
    assert response.json()["data"] == {**payload, "backend_id": owner.backend_id, "source": "live",
        "platform": sys.platform, "channel_state": "unavailable", "reason": "runtime_missing", "runtime": None}
    owner.close_admission()
    result = client.post("/api/internal/extension-hosts/resolve", json=payload, headers=headers)
    assert result.json()["data"]["channel_state"] == "draining"
    assert result.json()["data"]["reason"] == "backend_draining"
    for wrong in ({"backend_id": str(uuid4())}, {"workspace_ids": [str(uuid4())]}):
        assert client.post("/api/internal/extension-hosts/resolve", json={**payload, **wrong}, headers=headers).status_code == 409
    assert owner.active == () and not (owner._home / "state/backend/extension-hosts").exists()


@pytest.mark.parametrize(("headers", "status"), [
    ({}, 401),
    ({"X-Local-Token": "local-dev-token"}, 401),
    ({"X-Local-Token": "private-host-token", "Origin": "https://browser.invalid"}, 403),
    ({"X-Local-Token": "private-host-token", "Sec-Fetch-Site": "same-origin"}, 403),
    ({"X-Local-Token": "private-host-token"}, 400),
])
def test_private_discovery_preserves_control_boundary(
    private_host_client: tuple[TestClient, WorkspaceContainerRegistry], headers: dict[str, str], status: int,
) -> None:
    client, registry = private_host_client
    payload = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32,
               "workspace_ids": list(registry.mounted_workspaces.workspace_ids)}
    response = client.post("/api/internal/extension-hosts/resolve", json=payload, headers=headers)
    assert response.status_code == status and registry.extension_hosts.active == ()


def test_private_target_resolve_requires_configured_management_client(
    private_host_client: tuple[TestClient, WorkspaceContainerRegistry],
) -> None:
    client, registry = private_host_client
    payload = {"protocol_version": 1, "gateway_id": "gateway_" + "a" * 32,
        "workspace_ids": list(registry.mounted_workspaces.workspace_ids)}
    headers = {"X-Local-Token": "private-host-token", "X-BoxTeam-Gateway-Id": payload["gateway_id"],
        "X-Request-ID": "target-identity"}
    response = client.post("/api/internal/extension-targets/resolve", json=payload, headers=headers)
    assert response.status_code == 409
    owner = registry.extension_hosts
    assert owner.active == () and owner._management is None
    assert not (owner._home / "state/backend/extension-management").exists()
    assert not (owner._home / "state/backend/extension-hosts").exists()

    for wrong in ({"backend_id": str(uuid4())}, {"workspace_ids": [str(uuid4())]}):
        rejected = client.post("/api/internal/extension-targets/resolve", json={**payload, **wrong}, headers=headers)
        assert rejected.status_code == 409
    assert owner.active == () and owner._management is None

    rejected_gateway = client.post("/api/internal/extension-targets/resolve", json={**payload, "gateway_id": "gateway_" + "c" * 32}, headers=headers)
    assert rejected_gateway.status_code == 400 and owner.active == ()
