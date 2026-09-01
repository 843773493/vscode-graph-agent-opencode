import asyncio
from pathlib import Path

import pytest

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.controller import (
    GatewayWorkspaceRuntimeController,
    reconnect_gateway_workspace,
)
from app.gateway.runtime.local_workspace import (
    _adopt_browser_manager,
    _adopt_terminal_manager,
    _adopt_workspace_backend,
    restart_managed_workspace_backend,
    start_managed_local_workspace_runtime,
)
from app.gateway.runtime.workspace import WorkspaceRuntime


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
async def test_runtime_controller_starts_and_stops_optional_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="default",
            name="Default",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            managed=True,
            removable=False,
            system_default=True,
        ),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://127.0.0.1:41000"}),
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id="optional",
            name="Optional",
            root_path=str(tmp_path),
            backend_url="",
            connection_kind="local",
            managed=True,
        ),
        activate=False,
    )

    async def fake_start(**_: object) -> WorkspaceRuntime:
            return WorkspaceRuntime(
                service_urls={
                    "workspace_api": "http://127.0.0.1:42000",
                    "terminal_manager": "http://127.0.0.1:42001",
                    "browser_manager": "http://127.0.0.1:42002",
                }
            )

    async def fake_list_dtos() -> list[object]:
        return []

    monkeypatch.setattr(
        "app.gateway.runtime.controller.start_managed_local_workspace_runtime",
        fake_start,
    )
    monkeypatch.setattr(registry, "list_dtos", fake_list_dtos)
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )
    async def fake_runtime_action(*_: object, **__: object) -> dict[str, object]:
        return {"blockers": []}

    async def fake_runtime_status(*_: object, **__: object) -> dict[str, object]:
        return {"blockers": []}

    monkeypatch.setattr(controller, "_runtime_action", fake_runtime_action)
    monkeypatch.setattr(controller, "_runtime_status", fake_runtime_status)

    started = await controller.start_managed_backend(
        "optional",
        request_id="req_start",
    )
    assert started.status == "started"
    assert registry.has_runtime("optional") is True
    assert registry.resolve("optional").backend_url == "http://127.0.0.1:42000"

    stopped = await controller.stop_managed_backend(
        "optional",
        request_id="req_stop",
    )
    assert stopped.status == "stopped"
    assert registry.has_runtime("optional") is False

    await controller.start_managed_backend("optional", request_id="req_restart")
    controller._drain_timeout_seconds = 0

    async def blocked_runtime_status(
        *_: object,
        **__: object,
    ) -> dict[str, object]:
        return {
            "blockers": [
                {
                    "kind": "job",
                    "resource_id": "job_running",
                    "session_id": "session_running",
                    "status": "running",
                }
            ]
        }

    monkeypatch.setattr(controller, "_runtime_status", blocked_runtime_status)
    blocked = await controller.stop_managed_backend(
        "optional",
        request_id="req_blocked",
    )
    assert blocked.status == "blocked"
    assert blocked.blockers[0].resource_id == "job_running"
    assert registry.has_runtime("optional") is True

    with pytest.raises(PermissionError, match="默认工作区不能关闭"):
        await controller.stop_managed_backend("default", request_id="req_default")


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
        self.detached = False
        self.terminate_requested = False
        self.process = object()

    def close(self, *, timeout_seconds: float = 8) -> None:
        self.closed = True

    def request_terminate(self) -> None:
        self.terminate_requested = True

    def detach(self) -> None:
        self.detached = True


def test_runtime_replacement_hands_off_reused_browser_manager(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    target = WorkspaceTarget(
        workspace_id="gw_managed",
        name="Managed",
        root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000",
        connection_kind="local",
        managed=True,
    )
    old_backend = _RuntimeProcess()
    old_browser = _RuntimeProcess()
    browser_url = "http://127.0.0.1:41002"
    registry.upsert(
        target,
        runtime=WorkspaceRuntime(
            service_urls={
                "workspace_api": target.backend_url,
                "browser_manager": browser_url,
            },
            processes={
                "workspace_api": old_backend,
                "browser_manager": old_browser,
            },
        ),
    )

    replacement_browser = _RuntimeProcess()
    replacement = WorkspaceRuntime(
        service_urls={
            "workspace_api": "http://127.0.0.1:42000",
            "browser_manager": browser_url,
        },
        processes={"browser_manager": replacement_browser},
    )
    registry.upsert(target, runtime=replacement)

    assert old_browser.detached is True
    assert old_browser.terminate_requested is False
    assert old_browser.closed is False
    assert old_backend.terminate_requested is True
    assert old_backend.closed is True
    assert registry.managed_runtime(target.workspace_id) is replacement


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


def test_gateway_restart_detaches_browser_and_closes_other_services() -> None:
    backend = _RuntimeProcess()
    terminal = _RuntimeProcess()
    browser = _RuntimeProcess()
    runtime = WorkspaceRuntime(
        service_urls={
            "workspace_api": "http://127.0.0.1:41000",
            "terminal_manager": "http://127.0.0.1:41001",
            "browser_manager": "http://127.0.0.1:41002",
        },
        processes={
            "workspace_api": backend,
            "terminal_manager": terminal,
            "browser_manager": browser,
        },
    )

    runtime.close_for_gateway_restart()

    assert backend.closed is True
    assert terminal.closed is True
    assert browser.closed is False
    assert browser.detached is True
    assert runtime.processes == {}


@pytest.mark.asyncio
async def test_gateway_adopts_matching_browser_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "ok": True,
                "process_id": 43210,
                "workspace_root": str(tmp_path.resolve()),
            }

    class Client:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 2

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> Response:
            assert url == "http://127.0.0.1:42002/health"
            return Response()

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.httpx.AsyncClient",
        Client,
    )

    adopted = await _adopt_browser_manager(
        service_url="http://127.0.0.1:42002",
        workspace_root=tmp_path,
    )

    assert adopted is not None
    assert adopted.pid == 43210


@pytest.mark.asyncio
async def test_gateway_rejects_persisted_browser_manager_url_without_port(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="缺少端口"):
        await _adopt_browser_manager(
            service_url="http://127.0.0.1",
            workspace_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_gateway_adopts_matching_terminal_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "ok": True,
                "process_id": 43211,
                "workspace_root": str(tmp_path.resolve()),
            }

    class Client:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 2

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> Response:
            assert url == "http://127.0.0.1:42001/health"
            return Response()

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.httpx.AsyncClient",
        Client,
    )

    adopted = await _adopt_terminal_manager(
        service_url="http://127.0.0.1:42001",
        workspace_root=tmp_path,
    )

    assert adopted is not None
    assert adopted.pid == 43211


@pytest.mark.asyncio
async def test_gateway_adopts_matching_workspace_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200
        text = "ok"

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "status": "ok",
                "process_id": 43212,
                "workspace_root": str(tmp_path.resolve()),
            }

    class Client:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 2

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> Response:
            assert url == "http://127.0.0.1:42003/api/v1/health"
            return Response()

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.httpx.AsyncClient",
        Client,
    )

    adopted = await _adopt_workspace_backend(
        service_url="http://127.0.0.1:42003",
        workspace_root=tmp_path,
    )

    assert adopted is not None
    assert adopted.pid == 43212


@pytest.mark.asyncio
async def test_managed_runtime_reuses_terminal_and_browser_managers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = iter([41000])
    backend = _RuntimeProcess()
    adopted_terminal = _RuntimeProcess()
    adopted_browser = _RuntimeProcess()
    started_services: list[str] = []

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.allocate_local_port",
        lambda: next(ports),
    )

    async def adopt_terminal(**_: object) -> _RuntimeProcess:
        return adopted_terminal

    async def adopt_browser(**_: object) -> _RuntimeProcess:
        return adopted_browser

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_terminal_manager",
        adopt_terminal,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_browser_manager",
        adopt_browser,
    )

    def start_node(**kwargs: object) -> _RuntimeProcess:
        started_services.append(str(kwargs["service"]))
        return _RuntimeProcess()

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_node_service_process",
        start_node,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        lambda **_: backend,
    )

    async def ready(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_http_ok",
        ready,
    )

    runtime = await start_managed_local_workspace_runtime(
        project_root=tmp_path,
        workspace_root=tmp_path,
        log_dir=tmp_path / "logs",
        reusable_service_urls={
            "terminal_manager": "http://127.0.0.1:42001",
            "browser_manager": "http://127.0.0.1:42002",
        },
    )

    assert started_services == []
    assert runtime.service_urls["terminal_manager"] == "http://127.0.0.1:42001"
    assert runtime.service_urls["browser_manager"] == "http://127.0.0.1:42002"
    assert runtime.processes["terminal_manager"] is adopted_terminal
    assert runtime.processes["browser_manager"] is adopted_browser


@pytest.mark.asyncio
async def test_managed_runtime_adopts_workspace_backend_after_gateway_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adopted_backend = _RuntimeProcess()
    adopted_terminal = _RuntimeProcess()
    adopted_browser = _RuntimeProcess()

    async def adopt_backend(**_: object) -> _RuntimeProcess:
        return adopted_backend

    async def adopt_terminal(**_: object) -> _RuntimeProcess:
        return adopted_terminal

    async def adopt_browser(**_: object) -> _RuntimeProcess:
        return adopted_browser

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_workspace_backend",
        adopt_backend,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_terminal_manager",
        adopt_terminal,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_browser_manager",
        adopt_browser,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.allocate_local_port",
        lambda: pytest.fail("接管完整运行时不应重新分配端口"),
    )

    async def ready(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_http_ok",
        ready,
    )

    runtime = await start_managed_local_workspace_runtime(
        project_root=tmp_path,
        workspace_root=tmp_path,
        log_dir=tmp_path / "logs",
        reusable_backend_url="http://127.0.0.1:42000",
        reusable_service_urls={
            "terminal_manager": "http://127.0.0.1:42001",
            "browser_manager": "http://127.0.0.1:42002",
        },
    )

    assert runtime.service_urls == {
        "workspace_api": "http://127.0.0.1:42000",
        "terminal_manager": "http://127.0.0.1:42001",
        "browser_manager": "http://127.0.0.1:42002",
    }
    assert runtime.processes == {
        "workspace_api": adopted_backend,
        "terminal_manager": adopted_terminal,
        "browser_manager": adopted_browser,
    }


@pytest.mark.asyncio
async def test_managed_runtime_reclaims_persisted_backend_before_fresh_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = iter([42000, 42001, 42002])
    stale_backend = _RuntimeProcess()
    fresh_backend = _RuntimeProcess()
    started_services: list[str] = []

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.allocate_local_port",
        lambda: next(ports),
    )

    async def adopt_backend(**_: object) -> _RuntimeProcess:
        return stale_backend

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_workspace_backend",
        adopt_backend,
    )

    def start_node(**kwargs: object) -> _RuntimeProcess:
        started_services.append(str(kwargs["service"]))
        return _RuntimeProcess()

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_node_service_process",
        start_node,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        lambda **_: fresh_backend,
    )

    async def ready(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_http_ok",
        ready,
    )

    runtime = await start_managed_local_workspace_runtime(
        project_root=tmp_path,
        workspace_root=tmp_path,
        log_dir=tmp_path / "logs",
        reusable_backend_url="http://127.0.0.1:41999",
        adopt_existing_backend=False,
    )

    assert stale_backend.closed is True
    assert started_services == ["terminal", "browser"]
    assert runtime.service_urls["workspace_api"] == "http://127.0.0.1:42000"
    assert runtime.processes["workspace_api"] is fresh_backend


@pytest.mark.asyncio
async def test_managed_runtime_reuses_browser_without_starting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = iter([41000, 41001])
    backend = _RuntimeProcess()
    terminal = _RuntimeProcess()
    adopted_browser = _RuntimeProcess()
    started_services: list[str] = []

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.allocate_local_port",
        lambda: next(ports),
    )

    async def adopt(**_: object) -> _RuntimeProcess:
        return adopted_browser

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_browser_manager",
        adopt,
    )

    def start_node(**kwargs: object) -> _RuntimeProcess:
        started_services.append(str(kwargs["service"]))
        return terminal

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_node_service_process",
        start_node,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        lambda **_: backend,
    )

    async def ready(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_http_ok",
        ready,
    )

    runtime = await start_managed_local_workspace_runtime(
        project_root=tmp_path,
        workspace_root=tmp_path,
        log_dir=tmp_path / "logs",
        reusable_service_urls={
            "browser_manager": "http://127.0.0.1:42002"
        },
    )

    assert started_services == ["terminal"]
    assert runtime.service_urls["browser_manager"] == "http://127.0.0.1:42002"
    assert runtime.processes["browser_manager"] is adopted_browser
