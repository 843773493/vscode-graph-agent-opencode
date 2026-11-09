"""Gateway 按持久化完整 owner 恢复，并保留真实失败与清理结果。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core import failures
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)
from app.gateway import remote_gateway
from app.gateway.auth import verify_gateway_token
from app.gateway.config import (
    ConfiguredRemoteGateway,
    GatewayConfig,
    resolve_gateway_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import workspaces
from app.gateway.runtime.consumer_protocol import GatewayRuntimeConsumerApplyError
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import bootstrap
from tests.support.gateway_managed_backends import ProcessProbe


@dataclass
class StartupCase:
    gateway_root: Path
    default_root: Path
    identities: dict[Path, str] = field(default_factory=dict)
    validated_roots: list[tuple[tuple[Path, ...], bool]] = field(default_factory=list)
    started: list[tuple[object, dict[str, object]]] = field(default_factory=list)
    handles: list[tuple[ProcessProbe, ProcessProbe]] = field(default_factory=list)
    backend_adopted: bool = False
    auxiliary_failure: Exception | None = None
    bad_fields: bool = False

    async def start(self, *, owner: ManagedBackendOwner, registry: GatewayWorkspaceRegistry, **options: object):
        mounts = registry.resolve_backend_mounts(owner.contract)
        for mount in mounts:
            if not mount.workspace_root.is_dir():
                raise FileNotFoundError(f"工作区目录不存在: {mount.workspace_root}")
        self.started.append((owner, options))
        port = 44000 + len(self.started) * 10
        runtimes = {}
        backend = ProcessProbe("backend")
        browser = ProcessProbe("browser", failure=self.auxiliary_failure)
        self.handles.append((backend, browser))
        owner.set_process(backend, adopted=self.backend_adopted)
        for index, member in enumerate(owner.contract.members):
            browser_url = f"http://127.0.0.1:{port + index + 1}"
            runtime = WorkspaceRuntime(
                service_urls={"workspace_api": f"http://127.0.0.1:{port}", "browser_manager": browser_url},
                backend_owner=owner, workspace_id=member.workspace_id,
                processes={"browser_manager": browser} if index == 0 else {},
            )
            if self.bad_fields:
                runtime.service_urls.pop("browser_manager")
            runtimes[member.workspace_id] = runtime
            owner.unpublished_runtimes[member.workspace_id] = runtime
        return replace(owner.contract, backend_url=f"http://127.0.0.1:{port}"), runtimes


@pytest.fixture
def startup_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StartupCase:
    case = StartupCase(tmp_path / "gateway", tmp_path / "default")
    case.default_root.mkdir()
    case.identities[case.default_root] = "00000000-0000-4000-8000-000000000001"
    for name in ("BOXTEAM_DEFAULT_BACKEND_URL", "BOXTEAM_DEFAULT_BACKEND_PORT", "BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: case.gateway_root)
    monkeypatch.setattr(bootstrap, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "_default_workspace_root", lambda: case.default_root)
    monkeypatch.setattr(bootstrap, "load_gateway_config", lambda: GatewayConfig())
    def identity(_project, root, **_):
        if not root.is_dir():
            raise FileNotFoundError(f"工作区目录不存在: {root}")
        return case.identities[root]
    def identities(project: Path, roots: tuple[Path, ...], *, initialize: bool) -> tuple[str, ...]:
        case.validated_roots.append((roots, initialize))
        return tuple(identity(project, root) for root in roots)
    monkeypatch.setattr("app.gateway.registry.runtime.resolve_backend_workspace_id", identity)
    monkeypatch.setattr("app.gateway.registry.runtime.resolve_backend_workspace_ids", identities)
    monkeypatch.setattr(bootstrap, "start_managed_backend_runtime", case.start)
    return case


@pytest.fixture
def restore_state_store(request: pytest.FixtureRequest, startup_case: StartupCase) -> Iterator[GatewayStateStore | None]:
    if getattr(request, "param", False):
        with GatewayStateStore(path=startup_case.gateway_root / "restore.sqlite") as state:
            yield state
    else:
        yield None


@pytest.fixture
def persisted_targets(startup_case: StartupCase, managed_backend_factory, restore_state_store):
    case = startup_case
    registry = GatewayWorkspaceRegistry(storage_path=case.gateway_root / "workspaces.json", state_store=restore_state_store)
    count = 1

    def add(names: tuple[str, ...], *, desired_running: bool = True, default: bool = False, missing: bool = False, owner: bool = True):
        nonlocal count
        targets = []
        for name in names:
            root = case.default_root if default else case.default_root.parent / name
            if not missing:
                root.mkdir(exist_ok=True)
            if root not in case.identities:
                count += 1
                case.identities[root] = f"00000000-0000-4000-8000-{count:012d}"
            target = WorkspaceTarget(
                gateway_id=registry.gateway_id, workspace_id=case.identities[root], name=name,
                root_path=str(root), backend_url="http://127.0.0.1:41000",
                connection_kind="local", managed=True,
                owner="system" if default else "manual", system_default=default, removable=not default,
                desired_running=desired_running,
            )
            registry.upsert(target, activate=False)
            targets.append(target)
        graph = managed_backend_factory(tuple(targets), backend_url=targets[0].backend_url)
        if owner:
            registry.register_backend_owner(graph.owner)
        return targets, graph
    return registry, add


@pytest.fixture(params=["/private/restore-failure", r"C:\private\restore-failure", r"\\server\private\restore-failure"])
def native_restore_location(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=[False, True], ids=["runtime", "native-io"])
def restore_error(request: pytest.FixtureRequest, native_restore_location: str) -> Exception:
    if request.param:
        return OSError(5, "restore rejected", native_restore_location)
    return RuntimeError(f"restore rejected: {native_restore_location}")


@pytest.fixture(params=[False, True], ids=["log", "failed-log"])
def restore_sink_error(request: pytest.FixtureRequest, native_restore_location: str, monkeypatch: pytest.MonkeyPatch) -> OSError | None:
    if not request.param:
        return None
    error = OSError(5, "diagnostic sink failed", native_restore_location)

    def reject_log(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(failures.logger, "error", reject_log)
    return error


@pytest.fixture
def list_workspace_http():
    async def listed(registry: GatewayWorkspaceRegistry) -> httpx.Response:
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.include_router(workspaces.router)
        app.dependency_overrides[verify_gateway_token] = lambda: "test-token"
        app.dependency_overrides[workspaces.get_registry] = lambda: registry
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.get("/api/gateway/workspaces", params={"check_health": False}, headers={"X-Request-ID": "restore-failure-01"})
        assert response.status_code == 200, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "restore-failure-01"
        return response
    return listed


@pytest.fixture
def configured_remote_restore_case(request: pytest.FixtureRequest, startup_case: StartupCase, persisted_targets):
    persisted, _ = persisted_targets
    configured = []
    connections = []
    projections: dict[str, tuple[WorkspaceTarget, ...]] = {}
    for index, name in enumerate(("failed", "healthy"), start=1):
        item = ConfiguredRemoteGateway(
            connection_id=f"rgw_{name}", name=name, host=f"{name}.example.com",
            username="developer", private_key_path=str(startup_case.gateway_root / "keys" / name),
            ssh_config_host=name, remote_pair_command="boxteam gateway pair", activate=index == 1,
        )
        connection = RemoteGatewayConnection(
            connection_id=item.connection_id, name=name, host=item.host, port=item.port,
            username=item.username, private_key_path=str(resolve_gateway_path(item.private_key_path)),
            ssh_config_host=item.ssh_config_host, remote_gateway_port=item.remote_gateway_port,
            remote_pair_command=item.remote_pair_command, remote_gateway_id="gateway_" + str(index) * 32,
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            source_owner="manual" if getattr(request, "param", "config") == "manual" else "config",
            remote_config_event_cursor=index * 7,
        )
        targets = tuple(WorkspaceTarget(
            gateway_id=connection.remote_gateway_id,
            workspace_id=f"00000000-0000-4000-8000-{member + 1:012d}",
            name=f"{name}-{member}", root_path=None,
            backend_url=f"http://127.0.0.1:{43000 + index}", connection_kind="remote_gateway",
            owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,),
            remote_service_names=("workspace_api",),
        ) for member in range(2 if index == 1 else 1))
        persisted.apply_remote_projection_snapshot(connection=connection, projections=targets)
        projections[connection.connection_id] = tuple(replace(persisted.resolve(target.key)) for target in targets)
        configured.append(item)
        connections.append(connection)
    persisted.activate(projections[connections[0].connection_id][1].key)
    return GatewayConfig(workspaces=tuple(configured)), tuple(connections), projections


def test_default_backend_port_is_optional_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOXTEAM_DEFAULT_BACKEND_PORT", raising=False)
    assert bootstrap._default_backend_port() is None
    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_PORT", "8010")
    assert bootstrap._default_backend_port() == 8010
    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_PORT", "70000")
    with pytest.raises(ValueError, match="BOXTEAM_DEFAULT_BACKEND_PORT"):
        bootstrap._default_backend_port()


@pytest.mark.asyncio
async def test_gateway_start_restores_all_desired_managed_workspaces(startup_case, persisted_targets) -> None:
    persisted, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    running, _ = add(("Running-a", "Running-b"))
    stopped, _ = add(("Stopped",), desired_running=False)
    external_root = startup_case.default_root.parent / "External"
    external_root.mkdir()
    startup_case.identities[external_root] = "00000000-0000-4000-8000-000000000099"
    persisted.upsert(WorkspaceTarget(
        gateway_id=persisted.gateway_id, workspace_id=startup_case.identities[external_root],
        name="External", root_path=str(external_root),
        backend_url="http://127.0.0.1:42000", connection_kind="local",
        managed=False,
    ), activate=False)
    expected_roots = tuple(Path(target.root_path) for target in persisted.targets())
    persisted.activate(running[1].workspace_id)
    registry = await bootstrap.create_registry()
    assert startup_case.validated_roots == [(expected_roots, False)]
    assert not startup_case.started
    assert all(not registry.has_runtime(target.workspace_id) for target in defaults + running)
    assert all(registry.backend_owner(target.workspace_id) is not None for target in defaults + running)
    # 模拟 ManagedHTTPServer 在 listener 就绪后调用恢复阶段。
    await bootstrap._restore_managed_local_runtimes(
        registry=registry, default_workspace_id=defaults[0].workspace_id,
    )
    assert startup_case.validated_roots == [(expected_roots, False)] * 2
    assert len(startup_case.started) == 2
    assert registry.has_runtime(defaults[0].workspace_id)
    restored_owner = registry.backend_owner(running[0].workspace_id)
    assert restored_owner is registry.backend_owner(running[1].workspace_id)
    assert all(registry.managed_runtime(target.workspace_id).backend_owner is restored_owner for target in running)
    assert registry.active_workspace == (registry.gateway_id, running[1].workspace_id)
    assert not registry.has_runtime(stopped[0].workspace_id)
    assert registry.resolve(stopped[0].workspace_id).desired_running is False
    registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_state_store", [True], indirect=True)
async def test_gateway_non_root_start_failure_keeps_intent_and_restores_other_targets(
    startup_case, persisted_targets, monkeypatch, caplog, restore_state_store,
    restore_error: Exception, restore_sink_error: OSError | None,
    native_restore_location: str, list_workspace_http,
) -> None:
    registry, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    unavailable, graph = add(("Unavailable-a", "Unavailable-b"))
    healthy, _ = add(("Healthy",))
    registry.activate(unavailable[0].workspace_id)

    async def start(*, owner, **options):
        if owner is graph.owner:
            raise restore_error
        return await startup_case.start(owner=owner, **options)

    monkeypatch.setattr(bootstrap, "start_managed_backend_runtime", start)
    await bootstrap._restore_managed_local_runtimes(
        registry=registry, default_workspace_id=defaults[0].workspace_id,
    )
    assert all(registry.resolve(target.workspace_id).desired_running for target in unavailable)
    expected = f"Gateway 启动恢复完整 Backend 失败: owner={graph.owner.contract.owner_id}: stage=backend_restore, error={type(restore_error).__name__}"
    if isinstance(restore_error, OSError):
        expected += ", errno=5"
    if restore_sink_error is not None:
        expected += ", diagnostic_sink_error=OSError"
    assert graph.owner.error == expected
    assert all(registry.resolve(target.workspace_id).connection_error == expected for target in unavailable)
    if restore_sink_error is not None:
        assert any(str(restore_sink_error) in note for note in restore_error.__notes__)
    else:
        assert any(record.exc_info and record.exc_info[1] is restore_error for record in caplog.records)
    assert registry.has_runtime(healthy[0].workspace_id)
    assert registry.active_workspace == (registry.gateway_id, defaults[0].workspace_id)
    assert any("已回退到默认工作区" in record.getMessage() for record in caplog.records)
    listed = await list_workspace_http(registry)
    assert native_restore_location not in listed.text
    by_id = {item["workspace_id"]: item for item in listed.json()["data"]["items"]}
    assert all(by_id[member_id]["connection_error"] == expected for member_id in graph.owner.contract.workspace_ids)
    old_contract = graph.owner.contract
    registry.close()
    restore_state_store.close()
    # 关闭原writer后重新打开，遵守生产SQLite唯一写进程合同。
    with GatewayStateStore(path=startup_case.gateway_root / "restore.sqlite") as cold_state:
        cold = GatewayWorkspaceRegistry(storage_path=startup_case.gateway_root / "workspaces.json", state_store=cold_state)
        for target in unavailable:
            restored = cold.resolve(target.workspace_id)
            assert restored.connection_error == expected
            assert restored.desired_running and restored.key == target.key
            assert restored.root_path == target.root_path
        assert native_restore_location not in (await list_workspace_http(cold)).text
        monkeypatch.setattr(bootstrap, "start_managed_backend_runtime", startup_case.start)
        await bootstrap._restore_managed_local_runtimes(registry=cold, default_workspace_id=defaults[0].workspace_id)
        restored_owner = cold.backend_owner(unavailable[0].workspace_id)
        assert restored_owner is not None
        assert restored_owner.contract.generation == old_contract.generation
        assert restored_owner.contract.owner_id == old_contract.owner_id
        assert restored_owner.error is None
        assert all(cold.resolve(target.workspace_id).connection_error is None for target in unavailable)
        assert all(cold.managed_runtime(target.workspace_id).backend_owner is restored_owner for target in unavailable)
        cold.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_state_store", [True], indirect=True)
async def test_gateway_remote_restore_persists_failure_facts_and_retries_original_connection(
    startup_case, persisted_targets, restore_state_store, monkeypatch, caplog,
    restore_error: Exception, restore_sink_error: OSError | None,
    native_restore_location: str, list_workspace_http,
) -> None:
    persisted, _ = persisted_targets
    connections = []
    projections: dict[str, tuple[WorkspaceTarget, ...]] = {}
    for index, name in enumerate(("failed", "healthy"), start=1):
        connection = RemoteGatewayConnection(
            connection_id="rgw_" + str(index) * 32, name=name,
            host=f"{name}.example.com", port=22, username="developer",
            private_key_path=None, ssh_config_host=name, remote_gateway_port=8014,
            remote_gateway_id="gateway_" + str(index) * 32,
            protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        )
        targets = tuple(WorkspaceTarget(
            gateway_id=connection.remote_gateway_id,
            workspace_id=f"00000000-0000-4000-8000-{member + 2:012d}",
            name=f"{name}-{member}", root_path=None,
            backend_url=f"http://127.0.0.1:{43000 + index}", connection_kind="remote_gateway",
            owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,),
            remote_service_names=("workspace_api",),
        ) for member in range(2 if name == "failed" else 1))
        projections[connection.connection_id] = targets
        persisted.apply_remote_projection_snapshot(connection=connection, projections=targets)
        connections.append(connection)
    failed, healthy = connections
    attempts: list[str] = []
    fail_restore = True

    async def reconnect(*, registry, connection_id: str, **_options):
        attempts.append(connection_id)
        if connection_id == failed.connection_id and fail_restore:
            raise restore_error
        targets = projections[connection_id]
        registry.apply_remote_projection_snapshot(
            connection=registry.remote_gateway_connection(connection_id), projections=targets,
            runtime=WorkspaceRuntime(service_urls={"workspace_api": targets[0].backend_url}),
        )
        return targets

    monkeypatch.setattr(bootstrap, "reconnect_remote_gateway", reconnect)
    registry = await bootstrap.create_registry(state_store=restore_state_store)
    assert attempts == [failed.connection_id, healthy.connection_id]
    expected = f"恢复远程 Gateway 失败: connection_id={failed.connection_id}: stage=remote_gateway_restore, error={type(restore_error).__name__}"
    if isinstance(restore_error, OSError):
        expected += ", errno=5"
    if restore_sink_error is not None:
        expected += ", diagnostic_sink_error=OSError"
    failed_keys = tuple(target.key for target in projections[failed.connection_id])
    assert all(registry.resolve(key).connection_error == expected for key in failed_keys)
    healthy_target = projections[healthy.connection_id][0]
    assert registry.resolve_service_url(healthy_target.key, "workspace_api") == healthy_target.backend_url
    if restore_sink_error is not None:
        assert any(str(restore_sink_error) in note for note in restore_error.__notes__)
    else:
        assert any(record.exc_info and record.exc_info[1] is restore_error for record in caplog.records)
    listed = await list_workspace_http(registry)
    by_owner = {(item["gateway_id"], item["workspace_id"]): item for item in listed.json()["data"]["items"]}
    assert all(by_owner[key]["connection_error"] == expected and by_owner[key]["status"] == "offline" for key in failed_keys)
    assert by_owner[healthy_target.key]["status"] == "ready"
    assert native_restore_location not in listed.text
    registry.close()
    restore_state_store.close()
    with GatewayStateStore(path=startup_case.gateway_root / "restore.sqlite") as cold_state:
        cold = GatewayWorkspaceRegistry(storage_path=startup_case.gateway_root / "workspaces.json", state_store=cold_state)
        assert all(cold.resolve(key).connection_error == expected for key in failed_keys)
        assert cold.remote_gateway_connection(failed.connection_id) == failed
        assert native_restore_location not in (await list_workspace_http(cold)).text
        fail_restore = False
        restored = await bootstrap.create_registry(state_store=cold_state)
        assert attempts == [failed.connection_id, healthy.connection_id] * 2
        assert all(restored.resolve(key).connection_error is None for key in failed_keys)
        assert all(restored.resolve_service_url(key, "workspace_api") == projections[failed.connection_id][0].backend_url for key in failed_keys)
        assert all(restored.resolve(key).key == key for key in failed_keys)
        assert all(restored.resolve(key).remote_gateway_connection_ids == (failed.connection_id,) for key in failed_keys)
        assert restored.resolve(healthy_target.key).connection_error is None
        restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_state_store", [True], indirect=True)
async def test_unchanged_configured_remote_cold_restore_keeps_offline_owner_and_active(
    startup_case: StartupCase, configured_remote_restore_case, restore_state_store,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, list_workspace_http,
) -> None:
    config, connections, projections = configured_remote_restore_case
    failed, healthy = connections
    offline = httpx.ConnectError("configured peer offline: /private/remote-restore")
    prepared: list[str] = []
    attempts: list[str] = []
    fail_restore = True

    async def prepare(*, configured: ConfiguredRemoteGateway, **_options: object):
        prepared.append(configured.connection_id)
        raise offline

    async def reconnect(*, registry: GatewayWorkspaceRegistry, connection_id: str, **_options: object):
        attempts.append(connection_id)
        if connection_id == failed.connection_id and fail_restore:
            raise offline
        targets = projections[connection_id]
        registry.apply_remote_projection_snapshot(
            connection=registry.remote_gateway_connection(connection_id), projections=targets,
            runtime=WorkspaceRuntime(service_urls={"workspace_api": targets[0].backend_url}),
        )
        return targets

    monkeypatch.setattr(remote_gateway, "_prepare_remote_gateway", prepare)
    monkeypatch.setattr(bootstrap, "reconnect_remote_gateway", reconnect)
    registry = await bootstrap.create_registry(gateway_config=config, state_store=restore_state_store)
    expected = f"恢复远程 Gateway 失败: connection_id={failed.connection_id}: stage=remote_gateway_restore, error=ConnectError"
    failed_targets = projections[failed.connection_id]
    active = failed_targets[1].key
    assert prepared == []
    assert attempts == [failed.connection_id, healthy.connection_id]
    assert registry.active_workspace == active
    assert registry.remote_gateway_connection(failed.connection_id) == failed
    assert any(record.exc_info and record.exc_info[1] is offline for record in caplog.records)
    for target in failed_targets:
        current = registry.resolve(target.key)
        assert current.connection_error == expected
        assert replace(current, connection_error=target.connection_error) == target
    listed = await list_workspace_http(registry)
    by_owner = {(item["gateway_id"], item["workspace_id"]): item for item in listed.json()["data"]["items"]}
    assert listed.json()["data"]["active_workspace"] == {"gateway_id": active[0], "workspace_id": active[1]}
    assert all(by_owner[target.key]["status"] == "offline" and by_owner[target.key]["connection_error"] == expected for target in failed_targets)
    assert by_owner[projections[healthy.connection_id][0].key]["status"] == "ready"
    assert "/private/remote-restore" not in listed.text
    registry.close()
    restore_state_store.close()
    with GatewayStateStore(path=startup_case.gateway_root / "restore.sqlite") as cold_state:
        cold = GatewayWorkspaceRegistry(storage_path=startup_case.gateway_root / "workspaces.json", state_store=cold_state)
        assert cold.active_workspace == active
        assert cold.remote_gateway_connection(failed.connection_id) == failed
        for target in failed_targets:
            current = cold.resolve(target.key)
            assert current.connection_error == expected
            assert replace(current, connection_error=target.connection_error) == target
        cold.close()
        fail_restore = False
        restored = await bootstrap.create_registry(gateway_config=config, state_store=cold_state)
        assert attempts == [failed.connection_id, healthy.connection_id] * 2
        assert prepared == []
        assert restored.active_workspace == active
        assert restored.remote_gateway_connection(failed.connection_id) == failed
        assert all(restored.resolve(target.key) == target for target in failed_targets)
        assert all(restored.resolve_service_url(target.key, "workspace_api") == target.backend_url for target in failed_targets)
        restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_state_store", [True], indirect=True)
@pytest.mark.parametrize("boundary", [
    "candidate", "new", "removed", "duplicate", "name", "host", "port", "username",
    "private_key_path", "ssh_config_host", "remote_pair_command", "remote_gateway_port",
])
async def test_changed_configured_remote_or_candidate_still_requires_atomic_reconciliation(
    configured_remote_restore_case, restore_state_store, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    config, connections, projections = configured_remote_restore_case
    first, *remaining = config.workspaces
    if boundary == "new":
        config = replace(config, workspaces=(*config.workspaces, replace(first, connection_id="rgw_new")))
    elif boundary == "removed":
        config = replace(config, workspaces=(first,))
    elif boundary == "duplicate":
        config = replace(config, workspaces=(*config.workspaces, first))
    elif boundary != "candidate":
        value = getattr(first, boundary)
        changed = value + 1 if isinstance(value, int) else str(value) + "-changed"
        config = replace(config, workspaces=(replace(first, **{boundary: changed}), *remaining))
    offline = httpx.ConnectError("configured peer offline")
    prepared: list[str] = []
    restored: list[str] = []

    async def prepare(*, configured: ConfiguredRemoteGateway, **_options: object):
        prepared.append(configured.connection_id)
        raise offline

    async def reconnect(*, connection_id: str, **_options: object):
        restored.append(connection_id)
        raise AssertionError("新配置或候选不能走持久连接离线恢复")

    monkeypatch.setattr(remote_gateway, "_prepare_remote_gateway", prepare)
    monkeypatch.setattr(bootstrap, "reconnect_remote_gateway", reconnect)
    if boundary == "duplicate":
        with pytest.raises(ValueError, match="重复 connection_id"):
            await bootstrap.create_registry(gateway_config=config, state_store=restore_state_store)
        assert prepared == []
    else:
        with pytest.raises(GatewayRuntimeConsumerApplyError) as raised:
            await bootstrap.create_registry(
                gateway_config=config, state_store=restore_state_store,
                preserve_existing_managed_runtimes=boundary == "candidate",
            )
        assert raised.value.original_error is offline
        assert raised.value.__cause__ is offline
        assert raised.value.recovery_required is False
        assert prepared == [first.connection_id]
    assert restored == []
    cold = GatewayWorkspaceRegistry(storage_path=Path(restore_state_store.path).with_name("workspaces.json"), state_store=restore_state_store)
    assert cold.active_workspace == projections[connections[0].connection_id][1].key
    assert cold.remote_gateway_connections() == connections
    assert all(cold.resolve(target.key) == target for targets in projections.values() for target in targets)
    cold.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_state_store", [True], indirect=True)
@pytest.mark.parametrize("configured_remote_restore_case", ["manual"], indirect=True)
async def test_configured_remote_with_manual_connection_requires_reconciliation(
    configured_remote_restore_case, restore_state_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, _ = configured_remote_restore_case
    offline = httpx.ConnectError("configured peer offline")

    async def prepare(**_options: object):
        raise offline

    async def reconnect(**_options: object):
        raise AssertionError("不同来源的同 ID 连接不能走普通冷恢复")

    monkeypatch.setattr(remote_gateway, "_prepare_remote_gateway", prepare)
    monkeypatch.setattr(bootstrap, "reconnect_remote_gateway", reconnect)
    with pytest.raises(GatewayRuntimeConsumerApplyError) as raised:
        await bootstrap.create_registry(gateway_config=config, state_store=restore_state_store)
    assert raised.value.original_error is offline


@pytest.mark.asyncio
@pytest.mark.parametrize("desired_running", [True, False])
@pytest.mark.parametrize("failure", ["root-missing", "uuid-mismatch", "owner-missing"])
async def test_cold_restore_rejects_invalid_binding_before_any_write_or_process(
    startup_case, persisted_targets, desired_running, failure, monkeypatch,
) -> None:
    registry, add = persisted_targets
    targets, _ = add(("Invalid",), desired_running=desired_running, missing=failure == "root-missing", owner=failure != "owner-missing")
    target = targets[0]
    registry.activate(target.workspace_id)
    if failure == "uuid-mismatch":
        startup_case.identities[Path(target.root_path)] = "00000000-0000-4000-8000-000000000099"
    before = registry._storage_path.read_bytes()
    revision = registry.registry_revision
    default_calls = []
    def default_root():
        default_calls.append(True)
        raise AssertionError("无效 binding 不得解析或创建默认工作区")
    monkeypatch.setattr(bootstrap, "_default_workspace_root", default_root)
    with pytest.raises((RuntimeError, FileNotFoundError), match="目录不存在|UUID|完整成员集合"):
        await bootstrap.create_registry()
    assert registry._storage_path.read_bytes() == before
    assert registry.registry_revision == revision
    assert not startup_case.started and not startup_case.handles and not default_calls
    assert registry.active_workspace == (registry.gateway_id, target.workspace_id)
    assert registry.resolve(target.workspace_id).desired_running is desired_running
    assert not (Path(target.root_path) / ".boxteam").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("desired_running", [True, False])
@pytest.mark.parametrize("storage", ["sqlite", "json-migration"])
@pytest.mark.parametrize("failure", ["root-missing", "identity-missing", "uuid-mismatch", "owner-missing"])
async def test_cold_restore_real_identity_cli_preserves_registry_on_invalid_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    desired_running: bool, storage: str, failure: str,
) -> None:
    gateway_root = tmp_path / "gateway"
    root = tmp_path / "registered"
    root.mkdir()
    workspace_id = load_or_create_workspace_id(root)
    state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
    path = gateway_root / "workspaces.json"
    persisted = GatewayWorkspaceRegistry(storage_path=path, state_store=state if storage == "sqlite" else None)
    target = WorkspaceTarget(
        gateway_id=persisted.gateway_id, workspace_id=workspace_id, name="Registered",
        root_path=str(root), backend_url="", connection_kind="local", managed=True,
        desired_running=desired_running,
    )
    persisted.upsert(target)
    if failure != "owner-missing":
        persisted.register_backend_owner(ManagedBackendOwner(ManagedBackendContract(
            gateway_id=persisted.gateway_id, owner_id="cold-owner", generation="cold-generation",
            members=(ManagedBackendMember(target.workspace_id),), backend_url="",
        )))
    identity_path = workspace_identity_path(root)
    if failure in {"root-missing", "identity-missing"}:
        identity_path.unlink()
        identity_path.parent.rmdir()
        if failure == "root-missing":
            root.rmdir()
    elif failure == "uuid-mismatch":
        identity_path.write_text('{"workspace_id":"00000000-0000-4000-8000-000000000099"}\n')
    before = state.load_workspace_registry()
    revision = state.get_registry_revision()
    json_before = path.read_bytes() if path.exists() else None
    identity_before = identity_path.read_bytes() if identity_path.exists() else None
    default_root = tmp_path / "default-must-not-exist"
    monkeypatch.setattr(bootstrap, "get_gateway_root", lambda: gateway_root)
    monkeypatch.setenv("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT", str(default_root))
    try:
        with pytest.raises((RuntimeError, FileNotFoundError), match="UUID|完整成员集合|No such file"):
            await bootstrap.create_registry(gateway_config=GatewayConfig(), state_store=state)
        assert state.load_workspace_registry() == before
        assert state.get_registry_revision() == revision
        assert (path.read_bytes() if path.exists() else None) == json_before
        assert not path.with_name("workspaces.json.migrated.bak").exists()
        assert (identity_path.read_bytes() if identity_path.exists() else None) == identity_before
        assert not default_root.exists()
        if failure in {"root-missing", "identity-missing"}:
            assert not (root / ".boxteam").exists()
        else:
            assert identity_path.exists()
        assert not (gateway_root / "logs").exists()
        assert not persisted.backend_owners() or persisted.backend_owners()[0].process is None
    finally:
        state.close()


@pytest.mark.asyncio
async def test_restore_missing_owner_rejects_without_guessing_url_or_fallback(startup_case, persisted_targets) -> None:
    registry, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    orphan, _ = add(("Orphan",), owner=False)
    registry.activate(orphan[0].workspace_id)
    before = registry._storage_path.read_bytes()
    with pytest.raises(RuntimeError, match="完整成员集合"):
        await bootstrap._restore_managed_local_runtimes(
            registry=registry, default_workspace_id=defaults[0].workspace_id,
        )
    assert registry._storage_path.read_bytes() == before
    assert not startup_case.started
    assert registry.backend_owner(orphan[0].workspace_id) is None
    assert registry.active_workspace == (registry.gateway_id, orphan[0].workspace_id)


@pytest.mark.asyncio
async def test_startup_fallback_from_stopped_managed_workspace_logs_warning(startup_case, persisted_targets, caplog) -> None:
    persisted, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    stopped, _ = add(("Stopped",), desired_running=False)
    persisted.activate(stopped[0].workspace_id)
    registry = await bootstrap.create_registry()
    assert registry.active_workspace == (registry.gateway_id, defaults[0].workspace_id)
    assert any(record.levelname == "WARNING" and "已回退到默认工作区" in record.getMessage() and stopped[0].workspace_id in record.getMessage() for record in caplog.records)
    registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("preserve_existing_managed_runtimes", [False, True])
async def test_registry_close_reclaims_runtimes_restored_after_listener_startup(
    startup_case, cleanup_fails, preserve_existing_managed_runtimes,
) -> None:
    startup_case.backend_adopted = True
    if cleanup_fails:
        startup_case.auxiliary_failure = RuntimeError("owned workspace runtime close failed")
    registry = await bootstrap.create_registry(
        preserve_existing_managed_runtimes=preserve_existing_managed_runtimes,
    )
    assert not startup_case.started and not startup_case.handles
    default_workspace_id = next(target.workspace_id for target in registry.targets() if target.system_default)
    await bootstrap._restore_managed_local_runtimes(
        registry=registry, default_workspace_id=default_workspace_id,
        preserve_existing_managed_runtimes=preserve_existing_managed_runtimes,
    )
    assert len(startup_case.started) == 1
    backend, browser = startup_case.handles[0]
    if cleanup_fails:
        with pytest.raises(RuntimeError, match="owned workspace runtime close failed"):
            registry.close(detach_adopted_managed_processes=preserve_existing_managed_runtimes)
    else:
        registry.close(detach_adopted_managed_processes=preserve_existing_managed_runtimes)
    assert browser.events.count("close:browser") == 1 and not browser.detached
    assert backend.detached is preserve_existing_managed_runtimes
    assert backend.closed is not preserve_existing_managed_runtimes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["default_runtime_fields", "registry_publish"])
async def test_candidate_default_runtime_failure_detaches_adopted_processes(startup_case, monkeypatch, failure_point) -> None:
    startup_case.backend_adopted = True
    startup_case.bad_fields = failure_point == "default_runtime_fields"
    error = RuntimeError("candidate default workspace registry publish failed")
    if failure_point == "registry_publish":
        def fail_publish(*_):
            raise error
        monkeypatch.setattr(GatewayWorkspaceRegistry, "publish_backend_runtime", fail_publish)
    registry = await bootstrap.create_registry(preserve_existing_managed_runtimes=True)
    default_workspace_id = next(target.workspace_id for target in registry.targets() if target.system_default)
    with pytest.raises(RuntimeError) as raised:
        await bootstrap._restore_managed_local_runtimes(
            registry=registry, default_workspace_id=default_workspace_id,
            preserve_existing_managed_runtimes=True,
        )
    if failure_point == "registry_publish":
        assert raised.value is error
    else:
        assert "Browser Manager 地址" in str(raised.value)
    backend, browser = startup_case.handles[0]
    assert backend.detached and not backend.closed
    assert browser.closed and not browser.detached
    assert browser.events.count("close:browser") == 1
    registry.close(detach_adopted_managed_processes=True)


@pytest.mark.asyncio
async def test_default_external_backend_discards_only_stopped_singleton_owner(startup_case, persisted_targets, monkeypatch) -> None:
    _, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_URL", "http://127.0.0.1:41000")
    async def healthy(*_, **__):
        return {"workspace_id": startup_case.identities[startup_case.default_root], "backend_id": "00000000-0000-4000-8000-000000000099"}
    monkeypatch.setattr(bootstrap, "wait_for_workspace_health", healthy)
    registry = await bootstrap.create_registry()
    assert registry.backend_owner(defaults[0].workspace_id) is None
    assert registry.resolve(defaults[0].workspace_id).managed is False
    assert registry.resolve(defaults[0].workspace_id).backend_id is None
    await bootstrap._restore_managed_local_runtimes(
        registry=registry, default_workspace_id=defaults[0].workspace_id,
    )
    assert registry.resolve(defaults[0].workspace_id).backend_id == "00000000-0000-4000-8000-000000000099"
    assert not startup_case.started


@pytest.mark.asyncio
async def test_default_external_backend_cannot_replace_shared_owner(startup_case, persisted_targets, monkeypatch) -> None:
    registry, add = persisted_targets
    defaults, _ = add(("Default",), default=True, owner=False)
    siblings, _ = add(("Sibling",), owner=False)
    from tests.support.gateway_managed_backends import backend_graph
    graph = backend_graph(tuple(defaults + siblings), backend_url="http://127.0.0.1:41000")
    registry.register_backend_owner(graph.owner)
    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_URL", "http://127.0.0.1:41000")
    with pytest.raises(ValueError, match="不能替换共享"):
        await bootstrap.create_registry()
    assert not startup_case.started


@pytest.mark.asyncio
async def test_restore_commit_observer_error_keeps_published_unique_owner(
    startup_case, persisted_targets,
) -> None:
    registry, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    running, graph = add(("Running-a", "Running-b"))
    registry.activate(running[1].workspace_id)
    old_contract = graph.owner.contract
    failure = RuntimeError("恢复提交观察者失败")
    def observer() -> None:
        raise failure
    registry.add_commit_observer(observer)
    try:
        with pytest.raises(RuntimeError) as raised:
            await bootstrap._restore_managed_local_runtimes(
                registry=registry, default_workspace_id=defaults[0].workspace_id,
                only_workspace_ids=set(graph.owner.contract.workspace_ids),
            )
        assert raised.value is failure
        assert graph.owner.contract is not old_contract
        assert graph.owner.error is None
        assert graph.owner.process is startup_case.handles[0][0]
        assert not graph.owner.process.closed
        assert not graph.owner.unpublished_runtimes
        assert registry.active_workspace == (registry.gateway_id, running[1].workspace_id)
        assert all(registry.resolve(target.workspace_id).connection_error is None for target in running)
        persisted = GatewayWorkspaceRegistry(storage_path=registry._storage_path)
        restored_owner = persisted.backend_owner(running[0].workspace_id)
        assert restored_owner is not None
        assert restored_owner.contract == graph.owner.contract
    finally:
        registry.remove_commit_observer(observer)
        registry.close()


@pytest.mark.asyncio
async def test_default_publication_observer_error_defers_cleanup_to_startup_owner(
    startup_case, persisted_targets,
) -> None:
    registry, add = persisted_targets
    defaults, graph = add(("Default",), default=True)
    target = defaults[0]
    failure = RuntimeError("默认提交观察者失败")
    def observer() -> None:
        if registry.has_runtime(target.workspace_id):
            raise failure
    registry.add_commit_observer(observer)
    try:
        with pytest.raises(RuntimeError) as raised:
            await bootstrap._restore_managed_local_runtimes(
                registry=registry, default_workspace_id=target.workspace_id,
                only_workspace_ids={target.workspace_id},
            )
        assert raised.value is failure
        backend, browser = startup_case.handles[0]
        assert graph.owner.process is backend and not backend.closed
        assert not browser.closed
        assert not graph.owner.unpublished_runtimes
        assert registry.managed_runtime(target.workspace_id).backend_owner is graph.owner
    finally:
        registry.remove_commit_observer(observer)
        registry.close()
    assert backend.events.count("close:backend") == 1
    assert browser.events.count("close:browser") == 1
