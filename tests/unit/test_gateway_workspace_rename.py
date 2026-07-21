from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.gateway.auth import get_gateway_local_token
from app.gateway.config import GatewayConfig
from app.gateway.main import app, get_registry
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import bootstrap
from app.gateway.workspace_ids import (
    build_managed_local_workspace_id,
)


def _local_target(workspace_id: str = "workspace-local") -> WorkspaceTarget:
    return WorkspaceTarget(
        workspace_id=workspace_id,
        name="Old name",
        root_path="/workspace/local",
        backend_url="http://127.0.0.1:18010",
        connection_kind="local",
    )


def test_registry_rename_persists_across_reload(tmp_path: Path) -> None:
    storage_path = tmp_path / "workspaces.json"
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    registry.upsert(_local_target())

    renamed = registry.rename("workspace-local", "  New name  ")
    restored = GatewayWorkspaceRegistry(storage_path=storage_path)

    assert renamed.name == "New name"
    assert renamed.name_customized is True
    assert restored.resolve("workspace-local").name == "New name"
    assert restored.resolve("workspace-local").name_customized is True


@pytest.mark.asyncio
async def test_rename_workspace_endpoint_returns_complete_workspace_list(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(_local_target())
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            response = await client.patch(
                "/api/gateway/workspaces/workspace-local",
                headers={"X-Local-Token": get_gateway_local_token()},
                json={"name": "Renamed workspace"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["data"]["active_workspace_id"] == "workspace-local"
    assert response.json()["data"]["items"][0]["name"] == "Renamed workspace"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   "])
async def test_rename_workspace_endpoint_rejects_empty_name(
    tmp_path: Path,
    name: str,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(_local_target())
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            response = await client.patch(
                "/api/gateway/workspaces/workspace-local",
                headers={"X-Local-Token": get_gateway_local_token()},
                json={"name": name},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"] == "Gateway 工作区名称不能为空"


@pytest.mark.asyncio
async def test_rename_workspace_endpoint_returns_not_found_for_unknown_workspace(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            response = await client.patch(
                "/api/gateway/workspaces/unknown-workspace",
                headers={"X-Local-Token": get_gateway_local_token()},
                json={"name": "Renamed workspace"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "未知 Gateway 工作区" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rename_workspace_endpoint_rejects_extra_fields(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(_local_target())
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway.test",
        ) as client:
            response = await client.patch(
                "/api/gateway/workspaces/workspace-local",
                headers={"X-Local-Token": get_gateway_local_token()},
                json={
                    "name": "Renamed workspace",
                    "root_path": "/workspace/other",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert registry.resolve("workspace-local").name == "Old name"


@pytest.mark.asyncio
async def test_default_workspace_starts_as_home_and_keeps_renamed_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    default_root = tmp_path / "default-workspace"
    default_root.mkdir()
    backend_url = "http://127.0.0.1:18010"
    old_default_workspace_id = "gw_old_default"
    default_workspace_id = build_managed_local_workspace_id(str(default_root))
    persisted = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json"
    )
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=old_default_workspace_id,
            name="boxteam_workspace",
            root_path=str(default_root),
            backend_url=backend_url,
            connection_kind="local",
            removable=False,
            system_default=True,
        )
    )
    persisted.close()
    monkeypatch.delenv("BOXTEAM_DEFAULT_WORKSPACE_NAME", raising=False)
    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: gateway_root)
    monkeypatch.setattr(bootstrap, "_default_workspace_root", lambda: default_root)
    monkeypatch.setattr(bootstrap, "load_gateway_config", lambda _root: GatewayConfig())

    async def start_default_runtime(**_: object) -> WorkspaceRuntime:
        return WorkspaceRuntime(
            service_urls={
                "workspace_api": backend_url,
                "terminal_manager": "http://127.0.0.1:18012",
                "browser_manager": "http://127.0.0.1:18015",
            }
        )

    monkeypatch.setattr(
        bootstrap,
        "start_managed_local_workspace_runtime",
        start_default_runtime,
    )

    initial = await bootstrap.create_registry()
    assert initial.active_workspace_id == default_workspace_id
    assert initial.resolve(default_workspace_id).name == "home"
    assert initial.resolve(default_workspace_id).managed is True
    initial.rename(default_workspace_id, "My home")
    initial.close()

    restored = await bootstrap.create_registry()
    assert restored.resolve(default_workspace_id).name == "My home"
    restored.close()
