from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.gateway.config import ConfiguredSshWorkspace, GatewayConfig
from app.gateway.main import app, get_registry
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server import bootstrap
from app.gateway.workspace_ids import build_ssh_workspace_id, build_workspace_id


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
                headers={"X-Local-Token": "local-dev-token"},
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
                headers={"X-Local-Token": "local-dev-token"},
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
                headers={"X-Local-Token": "local-dev-token"},
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
                headers={"X-Local-Token": "local-dev-token"},
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
    default_workspace_id = build_workspace_id(
        "local",
        str(default_root),
        backend_url,
    )
    persisted = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json"
    )
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=default_workspace_id,
            name="boxteam_workspace",
            root_path=str(default_root),
            backend_url=backend_url,
            connection_kind="local",
            removable=False,
            system_default=True,
        )
    )
    persisted.close()
    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_URL", backend_url)
    monkeypatch.delenv("BOXTEAM_DEFAULT_WORKSPACE_NAME", raising=False)
    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: gateway_root)
    monkeypatch.setattr(bootstrap, "_default_workspace_root", lambda: default_root)
    monkeypatch.setattr(bootstrap, "load_gateway_config", lambda _root: GatewayConfig())

    initial = await bootstrap.create_registry()
    assert initial.active_workspace_id == default_workspace_id
    assert initial.resolve(default_workspace_id).name == "home"
    initial.rename(default_workspace_id, "My home")
    initial.close()

    restored = await bootstrap.create_registry()
    assert restored.resolve(default_workspace_id).name == "My home"
    restored.close()


@pytest.mark.asyncio
async def test_configured_workspace_keeps_renamed_value_after_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    default_root = tmp_path / "default-workspace"
    default_root.mkdir()
    configured = ConfiguredSshWorkspace(
        name="Configured name",
        host="remote.example.com",
        port=2222,
        username="developer",
        private_key_path="/tmp/id_ed25519",
        remote_workspace_path="/workspace/remote",
    )
    workspace_id = build_ssh_workspace_id(
        root_path=configured.remote_workspace_path,
        host=configured.host,
        port=configured.port,
        username=configured.username,
        remote_backend_host=configured.remote_backend_host,
        remote_backend_port=configured.remote_backend_port,
    )
    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_URL", raising=False)
    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: gateway_root)
    monkeypatch.setattr(bootstrap, "_default_workspace_root", lambda: default_root)
    monkeypatch.setattr(
        bootstrap,
        "load_gateway_config",
        lambda _root: GatewayConfig(workspaces=(configured,)),
    )

    async def register_configured_workspace(**kwargs: object) -> WorkspaceTarget:
        registry = kwargs["registry"]
        assert isinstance(registry, GatewayWorkspaceRegistry)
        name = kwargs["name"]
        assert isinstance(name, str)
        return registry.upsert(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=name,
                name_customized=bool(kwargs["name_customized"]),
                root_path=configured.remote_workspace_path,
                backend_url="http://127.0.0.1:41000",
                connection_kind="ssh",
            ),
            activate=bool(kwargs["activate"]),
        )

    monkeypatch.setattr(
        bootstrap,
        "register_ssh_workspace",
        register_configured_workspace,
    )

    initial = await bootstrap.create_registry()
    assert initial.resolve(workspace_id).name == "Configured name"
    initial.rename(workspace_id, "User renamed")
    initial.close()

    restored = await bootstrap.create_registry()
    assert restored.resolve(workspace_id).name == "User renamed"
    assert restored.resolve(workspace_id).name_customized is True
    restored.close()
