from pathlib import Path

import pytest

from app.gateway.config import GatewayConfig
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import bootstrap
from app.gateway.workspace_ids import build_managed_local_workspace_id


@pytest.fixture(autouse=True)
def _stub_backend_workspace_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap,
        "resolve_backend_workspace_id",
        lambda *_: "00000000-0000-4000-8000-000000000001",
    )


def test_default_backend_port_is_optional_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_PORT", raising=False)
    assert bootstrap._default_backend_port() is None

    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_PORT", "8010")
    assert bootstrap._default_backend_port() == 8010

    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_PORT", "70000")
    with pytest.raises(ValueError, match="BOXTEAM_DEFAULT_BACKEND_PORT"):
        bootstrap._default_backend_port()


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
    persisted = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=default_id,
            name="Default",
            root_path=str(default_root),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
            },
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
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
                backend_workspace_id="00000000-0000-4000-8000-000000000001",
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
            },
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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


@pytest.mark.asyncio
async def test_gateway_restore_failure_falls_back_from_active_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    default_root = tmp_path / "default"
    default_root.mkdir(parents=True)
    missing_root = tmp_path / "missing"
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="default",
            name="Default",
            root_path=str(default_root),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=True,
            system_default=True,
            desired_running=True,
        ),
        runtime=WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:41000",
                "terminal_manager": "http://127.0.0.1:41001",
                "browser_manager": "http://127.0.0.1:41002",
            }
        ),
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id="missing",
            name="Missing",
            root_path=str(missing_root),
            backend_url="http://127.0.0.1:42000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=True,
            desired_running=True,
        )
    )
    missing_root.mkdir(parents=True)
    registry.acquire_route_reference("missing", streaming=True)

    monkeypatch.setattr(bootstrap, "get_project_root", lambda: tmp_path)

    await bootstrap._restore_managed_local_runtimes(
        registry=registry,
        default_workspace_id="default",
        gateway_root=gateway_root,
    )

    assert registry.active_workspace_id == "default"
    assert registry.resolve("missing").desired_running is True
    assert registry.resolve("missing").connection_error is not None
    assert registry.has_runtime("default") is True
    registry.release_route_reference("missing", streaming=True)
    registry.close()


@pytest.mark.asyncio
async def test_startup_fallback_from_stopped_managed_workspace_logs_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """启动时回退激活工作区必须留日志，不能静默发生。

    persisted 的激活工作区是托管工作区、且 desired_running 为 False 时，启动阶段
    无法恢复它，只能回退到默认工作区；该回退对用户可见（会话树整体切换），
    必须像 _restore_managed_local_runtimes 的同语义回退一样给出 warning。
    """

    gateway_root = tmp_path / "gateway"
    default_root = tmp_path / "default"
    stopped_root = tmp_path / "stopped"
    for workspace_root in (default_root, stopped_root):
        workspace_root.mkdir(parents=True)

    default_id = build_managed_local_workspace_id(str(default_root))
    stopped_id = build_managed_local_workspace_id(str(stopped_root))
    persisted = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=default_id,
            name="Default",
            root_path=str(default_root),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=True,
            removable=False,
            system_default=True,
        )
    )
    persisted.upsert(
        WorkspaceTarget(
            workspace_id=stopped_id,
            name="Stopped",
            root_path=str(stopped_root),
            backend_url="http://127.0.0.1:42000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=True,
            desired_running=False,
        ),
    )
    persisted.close()

    async def fake_start_runtime(**_: object) -> WorkspaceRuntime:
        return WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:44000",
                "terminal_manager": "http://127.0.0.1:44001",
                "browser_manager": "http://127.0.0.1:44002",
            },
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
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

    assert registry.active_workspace_id == default_id
    assert any(
        record.levelname == "WARNING"
        and "已回退到默认工作区" in record.getMessage()
        and stopped_id in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
    registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("preserve_existing_managed_runtimes", [False, True])
async def test_create_registry_closes_runtime_when_later_startup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
    preserve_existing_managed_runtimes: bool,
) -> None:
    gateway_root = tmp_path / "gateway"
    default_root = tmp_path / "default"
    default_root.mkdir()
    startup_error = RuntimeError("configured remote Gateway reconciliation failed")
    cleanup_error = RuntimeError("owned workspace runtime close failed")

    class Process:
        def __init__(self, close_error: BaseException | None = None) -> None:
            self.close_error = close_error
            self.close_calls = 0
            self.detach_calls = 0

        def is_alive(self) -> bool:
            return True

        def request_terminate(self) -> None:
            return

        def close(self, *, timeout_seconds: float = 8) -> None:
            self.close_calls += 1
            if self.close_error is not None:
                raise self.close_error

        def detach(self) -> None:
            self.detach_calls += 1

    process = Process(cleanup_error if cleanup_fails else None)
    adopted_process = Process()

    async def fail_remote_reconciliation(**_: object) -> None:
        raise startup_error

    async def start_runtime(**_: object) -> WorkspaceRuntime:
        runtime = WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:44000",
                "terminal_manager": "http://127.0.0.1:44001",
                "browser_manager": "http://127.0.0.1:44002",
            },
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
        )
        runtime.set_process("workspace_api", adopted_process, adopted=True)
        runtime.set_process("terminal_manager", process)
        return runtime

    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_URL", raising=False)
    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_PORT", raising=False)
    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT", raising=False)
    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: gateway_root)
    monkeypatch.setattr(bootstrap, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "_default_workspace_root", lambda: default_root)
    monkeypatch.setattr(bootstrap, "load_gateway_config", lambda: GatewayConfig())
    monkeypatch.setattr(
        bootstrap, "start_managed_local_workspace_runtime", start_runtime
    )
    monkeypatch.setattr(
        bootstrap,
        "reconcile_configured_remote_gateways",
        fail_remote_reconciliation,
    )

    if cleanup_fails:
        with pytest.raises(BaseExceptionGroup) as raised:
            await bootstrap.create_registry(
                preserve_existing_managed_runtimes=preserve_existing_managed_runtimes
            )
        assert raised.value.exceptions[0] is startup_error
        assert "owned workspace runtime close failed" in str(raised.value.exceptions[1])
    else:
        with pytest.raises(RuntimeError) as raised:
            await bootstrap.create_registry(
                preserve_existing_managed_runtimes=preserve_existing_managed_runtimes
            )
        assert raised.value is startup_error

    assert process.close_calls == 1
    assert process.detach_calls == 0
    assert adopted_process.close_calls == (
        0 if preserve_existing_managed_runtimes else 1
    )
    assert adopted_process.detach_calls == (
        1 if preserve_existing_managed_runtimes else 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["default_runtime_fields", "registry_upsert"])
async def test_candidate_default_runtime_failure_detaches_adopted_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    gateway_root = tmp_path / "gateway"
    default_root = tmp_path / "default"
    default_root.mkdir()
    startup_error = RuntimeError("candidate default workspace registry upsert failed")

    class Process:
        def __init__(self) -> None:
            self.close_calls = 0
            self.detach_calls = 0

        def is_alive(self) -> bool:
            return True

        def request_terminate(self) -> None:
            return

        def close(self, *, timeout_seconds: float = 8) -> None:
            self.close_calls += 1

        def detach(self) -> None:
            self.detach_calls += 1

    adopted_process = Process()
    new_process = Process()

    async def start_runtime(**kwargs: object) -> WorkspaceRuntime:
        assert kwargs["adopt_existing_backend"] is True
        assert kwargs["preserve_adopted_processes_on_failure"] is True
        service_urls = {
            "terminal_manager": "http://127.0.0.1:44001",
            "browser_manager": "http://127.0.0.1:44002",
        }
        if failure_point != "default_runtime_fields":
            service_urls["workspace_api"] = "http://127.0.0.1:44000"
        runtime = WorkspaceRuntime(
            service_urls=service_urls,
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
        )
        runtime.set_process("workspace_api", adopted_process, adopted=True)
        runtime.set_process("terminal_manager", new_process)
        return runtime

    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_URL", raising=False)
    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_PORT", raising=False)
    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT", raising=False)
    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: gateway_root)
    monkeypatch.setattr(bootstrap, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "_default_workspace_root", lambda: default_root)
    monkeypatch.setattr(bootstrap, "load_gateway_config", lambda: GatewayConfig())
    monkeypatch.setattr(
        bootstrap, "start_managed_local_workspace_runtime", start_runtime
    )
    if failure_point == "registry_upsert":
        original_upsert = GatewayWorkspaceRegistry.upsert

        def fail_default_upsert(
            registry: GatewayWorkspaceRegistry,
            target: WorkspaceTarget,
            *,
            runtime: WorkspaceRuntime | None = None,
            **kwargs: object,
        ) -> WorkspaceTarget:
            if target.system_default and runtime is not None:
                raise startup_error
            return original_upsert(registry, target, runtime=runtime, **kwargs)

        monkeypatch.setattr(GatewayWorkspaceRegistry, "upsert", fail_default_upsert)

    if failure_point == "default_runtime_fields":
        with pytest.raises(KeyError, match="workspace_api"):
            await bootstrap.create_registry(preserve_existing_managed_runtimes=True)
    else:
        with pytest.raises(RuntimeError) as raised:
            await bootstrap.create_registry(preserve_existing_managed_runtimes=True)
        assert raised.value is startup_error

    assert adopted_process.detach_calls == 1
    assert adopted_process.close_calls == 0
    assert new_process.close_calls == 1
    assert new_process.detach_calls == 0
