import asyncio
from pathlib import Path

import pytest

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.runtime.controller import (
    GatewayWorkspaceRuntimeController,
    reconnect_gateway_workspace,
)
from app.gateway.runtime.local_workspace import restart_managed_workspace_backend


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
        "app.gateway.runtime.controller.start_managed_local_workspace_runtime",
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


@pytest.mark.asyncio
async def test_runtime_controller_rejects_restart_for_external_backend(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_external",
            name="External",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:8010",
            connection_kind="local",
            managed=False,
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:8010"}
        ),
    )
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(ValueError, match="Gateway 托管"):
        await controller.safe_restart_managed_backend(
            "gw_external",
            request_id="req_test",
        )


@pytest.mark.asyncio
async def test_runtime_controller_serializes_managed_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_managed",
            name="Managed",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            managed=True,
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
    )
    active = 0
    peak = 0

    async def fake_restart(**_: object) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    async def fake_runtime_action(*_: object, **__: object) -> dict[str, object]:
        return {"blockers": []}

    async def fake_runtime_status(*_: object, **__: object) -> dict[str, object]:
        return {"blockers": []}

    monkeypatch.setattr(
        "app.gateway.runtime.controller.restart_managed_workspace_backend",
        fake_restart,
    )
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )
    monkeypatch.setattr(controller, "_runtime_action", fake_runtime_action)
    monkeypatch.setattr(controller, "_runtime_status", fake_runtime_status)

    await asyncio.gather(
        controller.safe_restart_managed_backend(
            "gw_managed",
            request_id="req_one",
        ),
        controller.safe_restart_managed_backend(
            "gw_managed",
            request_id="req_two",
        ),
    )

    assert peak == 1


class _RuntimeProcess:
    def __init__(self) -> None:
        self.closed = False
        self.process = object()

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_backend_restart_preserves_terminal_and_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_backend = _RuntimeProcess()
    terminal = _RuntimeProcess()
    browser = _RuntimeProcess()
    new_backend = _RuntimeProcess()
    runtime = WorkspaceRuntime(
        service_urls={
            "workspace_api": "http://127.0.0.1:41000",
            "terminal_manager": "http://127.0.0.1:41001",
            "browser_manager": "http://127.0.0.1:41002",
        },
        processes={
            "workspace_api": old_backend,
            "terminal_manager": terminal,
            "browser_manager": browser,
        },
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        lambda **_: new_backend,
    )

    async def ready(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_http_ok",
        ready,
    )

    await restart_managed_workspace_backend(
        runtime=runtime,
        project_root=tmp_path,
        workspace_root=tmp_path,
        log_dir=tmp_path / "logs",
    )

    assert old_backend.closed is True
    assert terminal.closed is False
    assert browser.closed is False
    assert runtime.processes["workspace_api"] is new_backend
    assert runtime.processes["terminal_manager"] is terminal
    assert runtime.processes["browser_manager"] is browser
