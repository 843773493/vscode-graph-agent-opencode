from pathlib import Path

import pytest

from app.gateway.config import GatewayConfig
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import bootstrap
from app.gateway.workspace_ids import build_managed_local_workspace_id


@pytest.mark.asyncio
async def test_gateway_start_restores_all_desired_managed_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    default_root = tmp_path / "default"
    running_root = tmp_path / "running"
    stopped_root = tmp_path / "stopped"
    for workspace_root in (default_root, running_root, stopped_root):
        workspace_root.mkdir(parents=True)

    default_id = build_managed_local_workspace_id(str(default_root))
    running_id = build_managed_local_workspace_id(str(running_root))
    stopped_id = build_managed_local_workspace_id(str(stopped_root))
    persisted = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json"
    )
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=default_id,
            name="Default",
            root_path=str(default_root),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            managed=True,
            removable=False,
            system_default=True,
            desired_running=True,
        )
    )
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=running_id,
            name="Running",
            root_path=str(running_root),
            backend_url="http://127.0.0.1:42000",
            connection_kind="local",
            managed=True,
            desired_running=True,
        )
    )
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=stopped_id,
            name="Stopped",
            root_path=str(stopped_root),
            backend_url="http://127.0.0.1:43000",
            connection_kind="local",
            managed=True,
            desired_running=False,
        ),
        activate=False,
    )

    started_roots: list[Path] = []
    reusable_backend_urls: list[str | None] = []

    async def fake_start_runtime(
        *,
        workspace_root: Path,
        reusable_backend_url: str | None = None,
        **_: object,
    ) -> WorkspaceRuntime:
        started_roots.append(workspace_root)
        reusable_backend_urls.append(reusable_backend_url)
        port = 44000 + len(started_roots) * 10
        return WorkspaceRuntime(
            service_urls={
                "workspace_api": f"http://127.0.0.1:{port}",
                "terminal_manager": f"http://127.0.0.1:{port + 1}",
                "browser_manager": f"http://127.0.0.1:{port + 2}",
            }
        )

    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: gateway_root)
    monkeypatch.setattr(bootstrap, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "_default_workspace_root", lambda: default_root)
    monkeypatch.setattr(bootstrap, "load_gateway_config", lambda: GatewayConfig())
    monkeypatch.setattr(
        bootstrap,
        "start_managed_local_workspace_runtime",
        fake_start_runtime,
    )

    registry = await bootstrap.create_registry()

    assert started_roots == [default_root]
    assert reusable_backend_urls == ["http://127.0.0.1:41000"]
    assert registry.has_runtime(default_id) is True
    assert registry.has_runtime(running_id) is False
    assert registry.resolve(running_id).backend_url == "http://127.0.0.1:42000"
    await bootstrap._restore_managed_local_runtimes(
        registry=registry,
        default_workspace_id=default_id,
        gateway_root=gateway_root,
    )
    assert started_roots == [default_root, running_root]
    assert reusable_backend_urls == [
        "http://127.0.0.1:41000",
        "http://127.0.0.1:42000",
    ]
    assert registry.has_runtime(running_id) is True
    assert registry.has_runtime(stopped_id) is False
    assert registry.active_workspace_id == running_id
    assert registry.resolve(running_id).desired_running is True
    assert registry.resolve(running_id).local_service_urls == {
        "terminal_manager": "http://127.0.0.1:44021",
        "browser_manager": "http://127.0.0.1:44022",
    }
    assert registry.resolve(stopped_id).desired_running is False
    registry.close()


@pytest.mark.asyncio
async def test_gateway_restore_failure_keeps_intent_and_restores_other_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    healthy_root = tmp_path / "healthy"
    healthy_root.mkdir()
    missing_root = tmp_path / "missing"
    registry = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json"
    )
    for workspace_id, workspace_root in (
        ("missing", missing_root),
        ("healthy", healthy_root),
    ):
        registry.upsert(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=workspace_id,
                root_path=str(workspace_root),
                backend_url="http://127.0.0.1:41000",
                connection_kind="local",
                managed=True,
                desired_running=True,
            ),
            activate=False,
        )

    async def fake_start_runtime(**_: object) -> WorkspaceRuntime:
        return WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:42000",
                "terminal_manager": "http://127.0.0.1:42001",
                "browser_manager": "http://127.0.0.1:42002",
            }
        )

    monkeypatch.setattr(bootstrap, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "start_managed_local_workspace_runtime",
        fake_start_runtime,
    )

    await bootstrap._restore_managed_local_runtimes(
        registry=registry,
        default_workspace_id="default",
        gateway_root=gateway_root,
    )

    missing = registry.resolve("missing")
    assert missing.desired_running is True
    assert missing.connection_error is not None
    assert "工作区目录不存在" in missing.connection_error
    assert registry.has_runtime("healthy") is True
    registry.close()
