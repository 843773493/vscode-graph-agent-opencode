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
    workspace_config_proof_expectation,
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


def test_runtime_controller_updates_health_controller_config(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )

    controller.update_health_controller_config(
        request_timeout_seconds=4,
        poll_interval_seconds=1.5,
    )

    assert controller._health_request_timeout_seconds == 4
    assert controller._health_poll_interval_seconds == 1.5
    with pytest.raises(ValueError, match="request_timeout_seconds"):
        controller.update_health_controller_config(
            request_timeout_seconds=0,
            poll_interval_seconds=1,
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
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
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
async def test_runtime_controller_blocks_stop_for_gateway_proxy_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_streaming",
            name="Streaming",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            managed=True,
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
    )
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
        drain_timeout_seconds=0,
    )

    async def fake_runtime_action(*_: object, **__: object) -> dict[str, object]:
        return {"blockers": []}

    async def fake_runtime_status(*_: object, **__: object) -> dict[str, object]:
        return {"blockers": []}

    monkeypatch.setattr(controller, "_runtime_action", fake_runtime_action)
    monkeypatch.setattr(controller, "_runtime_status", fake_runtime_status)

    registry.acquire_route_reference("gw_streaming", streaming=True)
    blocked = await controller.stop_managed_backend(
        "gw_streaming",
        request_id="req_streaming",
    )

    assert blocked.status == "blocked"
    assert blocked.blockers[0].kind == "proxy_stream"
    assert blocked.blockers[0].resource_id == "gw_streaming:streams"
    assert registry.has_runtime("gw_streaming") is True
    registry.release_route_reference("gw_streaming", streaming=True)


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

    async def fake_pending_startup_contract(*_: object, **__: object):
        return None

    monkeypatch.setattr(
        controller,
        "_pending_startup_contract",
        fake_pending_startup_contract,
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


@pytest.mark.asyncio
async def test_runtime_controller_records_restart_failure_after_old_runtime_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )
    target = WorkspaceTarget(
        workspace_id="gw_recovery",
        name="Recovery",
        root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000",
        connection_kind="local",
        managed=True,
    )
    runtime = WorkspaceRuntime(
        service_urls={
            "workspace_api": target.backend_url,
            "terminal_manager": "http://127.0.0.1:41001",
            "browser_manager": "http://127.0.0.1:41002",
        }
    )
    recorded: dict[str, object] = {}

    async def fail_restart(**_: object) -> None:
        raise RuntimeError("新 generation 启动失败")

    async def recover_old_runtime(*_: object, **__: object) -> dict[str, object]:
        recorded["old_runtime_cancelled"] = True
        return {}

    async def record_failure(_backend_url: str, **kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(
        "app.gateway.runtime.controller.restart_managed_workspace_backend",
        fail_restart,
    )
    monkeypatch.setattr(controller, "_runtime_action", recover_old_runtime)
    monkeypatch.setattr(controller, "_record_pending_restart_failure", record_failure)

    with pytest.raises(RuntimeError, match="新 generation 启动失败"):
        await controller._restart_backend(
            target,
            runtime,
            startup_contract={
                "candidate_ref": "candidate-ref",
                "target_generation": "generation-new",
                "fencing_token": "fencing-token",
            },
            request_id="request-recovery",
        )

    assert recorded["old_runtime_cancelled"] is True
    assert recorded["old_runtime_recovered"] is True
    assert recorded["candidate_ref"] == "candidate-ref"


@pytest.mark.asyncio
async def test_runtime_controller_resolves_pending_after_candidate_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )
    captured: dict[str, object] = {}

    class _Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    class _Client:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
        ) -> _Response:
            captured["health_url"] = url
            captured["health_headers"] = headers
            return _Response(
                {
                    "config_proof": {
                        "config_domain": "workspace",
                        "loaded_source": "pending",
                    }
                }
            )

        async def post(
            self,
            url: str,
            *,
            params: dict[str, str],
            json: dict[str, object],
            headers: dict[str, str],
        ) -> _Response:
            captured["resolve_url"] = url
            captured["resolve_params"] = params
            captured["resolve_json"] = json
            captured["resolve_headers"] = headers
            return _Response({})

    monkeypatch.setattr(
        "app.gateway.runtime.controller.httpx.AsyncClient",
        _Client,
    )

    await controller._resolve_pending_restart(
        "http://127.0.0.1:42000",
        candidate_ref="candidate-ref",
        request_id="request-id",
    )

    assert captured["health_url"] == "http://127.0.0.1:42000/api/v1/health"
    assert captured["resolve_url"] == (
        "http://127.0.0.1:42000/api/v1/config/pending/resolve"
    )
    assert captured["resolve_params"] == {"candidate_ref": "candidate-ref"}
    assert captured["resolve_json"] == {
        "config_domain": "workspace",
        "loaded_source": "pending",
    }


@pytest.mark.asyncio
async def test_runtime_controller_enters_recovery_when_old_runtime_cannot_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )
    target = WorkspaceTarget(
        workspace_id="gw_recovery_failed",
        name="Recovery failed",
        root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000",
        connection_kind="local",
        managed=True,
    )
    runtime = WorkspaceRuntime(
        service_urls={
            "workspace_api": target.backend_url,
            "terminal_manager": "http://127.0.0.1:41001",
            "browser_manager": "http://127.0.0.1:41002",
        }
    )
    recorded: dict[str, object] = {}

    async def fail_restart(**_: object) -> None:
        raise RuntimeError("新 generation 启动失败")

    async def fail_recovery(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("旧 generation 无法恢复")

    async def record_failure(_backend_url: str, **kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(
        "app.gateway.runtime.controller.restart_managed_workspace_backend",
        fail_restart,
    )
    monkeypatch.setattr(controller, "_runtime_action", fail_recovery)
    monkeypatch.setattr(controller, "_record_pending_restart_failure", record_failure)

    with pytest.raises(RuntimeError, match="旧 generation 恢复协议失败"):
        await controller._restart_backend(
            target,
            runtime,
            startup_contract={
                "candidate_ref": "candidate-ref",
                "target_generation": "generation-new",
                "fencing_token": "fencing-token",
            },
            request_id="request-recovery-failed",
        )

    assert recorded["old_runtime_recovered"] is False


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


def test_workspace_config_proof_expectation_redacts_fencing_token() -> None:
    expectation = workspace_config_proof_expectation(
        {
            "candidate_id": "candidate-1",
            "pending_revision": 4,
            "candidate_digest": "candidate-digest",
            "effective_digest": "effective-digest",
            "target_generation": "generation-2",
            "fencing_token": "fence-secret",
            "secret_binding_digest": "binding-digest",
        }
    )

    assert expectation["loaded_source"] == "pending"
    assert expectation["candidate_id"] == "candidate-1"
    assert expectation["loaded_commit_revision"] == 4
    assert expectation["generation_id"] == "generation-2"
    assert expectation["fencing_token_digest"] != "fence-secret"
    assert "fencing_token" not in expectation


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


@pytest.mark.asyncio
async def test_backend_restart_failure_keeps_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_backend = _RuntimeProcess()
    new_backend = _RuntimeProcess()
    recovered_backend = _RuntimeProcess()
    runtime = WorkspaceRuntime(
        service_urls={
            "workspace_api": "http://127.0.0.1:41000",
            "terminal_manager": "http://127.0.0.1:41001",
            "browser_manager": "http://127.0.0.1:41002",
        },
        processes={"workspace_api": old_backend},
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.allocate_local_port",
        lambda: 42000,
    )
    started_backends = iter((new_backend, recovered_backend))
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        lambda **_: next(started_backends),
    )

    wait_calls = 0

    async def not_ready(*_: object, **__: object) -> None:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls > 1:
            return
        raise RuntimeError("candidate backend unhealthy")

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_http_ok",
        not_ready,
    )

    with pytest.raises(RuntimeError, match="candidate backend unhealthy"):
        await restart_managed_workspace_backend(
            runtime=runtime,
            project_root=tmp_path,
            workspace_root=tmp_path,
            log_dir=tmp_path / "logs",
        )

    assert old_backend.closed is True
    assert new_backend.closed is True
    assert recovered_backend.closed is False
    assert runtime.processes["workspace_api"] is recovered_backend
    assert runtime.service_urls["workspace_api"] == "http://127.0.0.1:41000"


@pytest.mark.asyncio
async def test_backend_restart_requires_matching_config_proof_before_old_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_backend = _RuntimeProcess()
    new_backend = _RuntimeProcess()
    recovered_backend = _RuntimeProcess()
    runtime = WorkspaceRuntime(
        service_urls={
            "workspace_api": "http://127.0.0.1:41000",
            "terminal_manager": "http://127.0.0.1:41001",
            "browser_manager": "http://127.0.0.1:41002",
        },
        processes={"workspace_api": old_backend},
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.allocate_local_port",
        lambda: 42000,
    )
    started_backends = iter((new_backend, recovered_backend))
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        lambda **_: next(started_backends),
    )

    async def mismatched_proof(*_: object, **__: object) -> None:
        raise RuntimeError("config proof mismatch")

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_workspace_config_proof",
        mismatched_proof,
    )

    async def recovered_ready(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.wait_for_http_ok",
        recovered_ready,
    )
    startup_contract = {
        "candidate_id": "candidate-1",
        "pending_revision": 2,
        "candidate_digest": "candidate-digest",
        "effective_digest": "effective-digest",
        "target_generation": "generation-2",
        "fencing_token": "fence-2",
        "secret_binding_digest": "binding-digest",
    }

    with pytest.raises(RuntimeError, match="config proof mismatch"):
        await restart_managed_workspace_backend(
            runtime=runtime,
            project_root=tmp_path,
            workspace_root=tmp_path,
            log_dir=tmp_path / "logs",
            config_candidate_ref="candidate-ref",
            config_generation="generation-2",
            config_fencing_token="fence-2",
            config_startup_contract=startup_contract,
        )

    assert old_backend.closed is True
    assert new_backend.closed is True
    assert recovered_backend.closed is False
    assert runtime.processes["workspace_api"] is recovered_backend


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
async def test_managed_runtime_migrates_default_backend_to_preferred_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_backend = _RuntimeProcess()
    fresh_backend = _RuntimeProcess()
    started_ports: list[tuple[str, int]] = []
    allocated_ports = iter([42001, 42002])

    async def adopt_backend(**_: object) -> _RuntimeProcess:
        return stale_backend

    async def no_adopted_service(**_: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_workspace_backend",
        adopt_backend,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_terminal_manager",
        no_adopted_service,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace._adopt_browser_manager",
        no_adopted_service,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.allocate_local_port",
        lambda: next(allocated_ports),
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.is_local_port_available",
        lambda port: port == 8010,
    )

    def start_node(**kwargs: object) -> _RuntimeProcess:
        started_ports.append((str(kwargs["service"]), int(kwargs["port"])))
        return _RuntimeProcess()

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_node_service_process",
        start_node,
    )

    def start_backend(**kwargs: object) -> _RuntimeProcess:
        started_ports.append(("backend", int(kwargs["port"])))
        return fresh_backend

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        start_backend,
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
        preferred_backend_port=8010,
        reusable_backend_url="http://127.0.0.1:41999",
    )

    assert stale_backend.closed is True
    assert started_ports == [("terminal", 42001), ("browser", 42002), ("backend", 8010)]
    assert runtime.service_urls["workspace_api"] == "http://127.0.0.1:8010"
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
        reusable_service_urls={"browser_manager": "http://127.0.0.1:42002"},
    )

    assert started_services == ["terminal"]
    assert runtime.service_urls["browser_manager"] == "http://127.0.0.1:42002"
    assert runtime.processes["browser_manager"] is adopted_browser
