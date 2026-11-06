"""Backend 私有宿主入口必须在创建进程前拒绝非管理通道。"""

from __future__ import annotations

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
