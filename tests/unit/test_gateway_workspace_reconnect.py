from pathlib import Path

import pytest

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.service_runtime import WorkspaceRuntime
from app.gateway.workspace_reconnect import reconnect_gateway_workspace


@pytest.mark.asyncio
async def test_reconnect_managed_local_workspace_keeps_stable_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    target = WorkspaceTarget(
        workspace_id="gw_stable",
        name="Managed",
        root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000",
        connection_kind="local",
        managed=True,
        connection_error="旧连接失败",
    )
    registry.upsert(
        target,
        runtime=WorkspaceRuntime(service_urls={"workspace_api": target.backend_url}),
    )
    replacement = WorkspaceRuntime(
        service_urls={
            "workspace_api": "http://127.0.0.1:42000",
            "terminal_manager": "http://127.0.0.1:42001",
            "browser_manager": "http://127.0.0.1:42002",
        }
    )

    async def fake_start(**_: object) -> WorkspaceRuntime:
        return replacement

    monkeypatch.setattr(
        "app.gateway.workspace_reconnect.start_managed_local_workspace_runtime",
        fake_start,
    )

    await reconnect_gateway_workspace(
        registry=registry,
        workspace_id="gw_stable",
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )

    reconnected = registry.resolve("gw_stable")
    assert reconnected.workspace_id == "gw_stable"
    assert reconnected.backend_url == "http://127.0.0.1:42000"
    assert reconnected.connection_error is None
    assert registry.resolve_service_url("gw_stable", "browser_manager") == (
        "http://127.0.0.1:42002"
    )
