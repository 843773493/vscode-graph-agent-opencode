"""Gateway 按持久化完整 owner 恢复，并保留真实失败与清理结果。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from app.gateway.config import GatewayConfig
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.managed_backend import ManagedBackendMember
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import bootstrap
from app.gateway.workspace_ids import build_managed_local_workspace_id
from tests.support.gateway_managed_backends import ProcessProbe


@dataclass
class StartupCase:
    gateway_root: Path
    default_root: Path
    identities: dict[Path, str] = field(default_factory=dict)
    started: list[tuple[object, dict[str, object]]] = field(default_factory=list)
    handles: list[tuple[ProcessProbe, ProcessProbe]] = field(default_factory=list)
    backend_adopted: bool = False
    auxiliary_failure: Exception | None = None
    bad_fields: bool = False

    async def start(self, *, owner, **options):
        for member in owner.contract.members:
            if not member.mount.workspace_root.is_dir():
                raise FileNotFoundError(f"工作区目录不存在: {member.mount.workspace_root}")
        self.started.append((owner, options))
        port = 44000 + len(self.started) * 10
        members, runtimes = [], {}
        backend = ProcessProbe("backend")
        terminal = ProcessProbe("terminal", failure=self.auxiliary_failure)
        self.handles.append((backend, terminal))
        owner.set_process(backend, adopted=self.backend_adopted)
        for index, member in enumerate(owner.contract.members):
            terminal_url, browser_url = f"http://127.0.0.1:{port + index * 2 + 1}", f"http://127.0.0.1:{port + index * 2 + 2}"
            members.append(ManagedBackendMember(member.workspace_id, replace(
                member.mount, terminal_backend_url=terminal_url, browser_backend_url=browser_url,
            )))
            runtime = WorkspaceRuntime(
                service_urls={"workspace_api": f"http://127.0.0.1:{port}", "terminal_manager": terminal_url, "browser_manager": browser_url},
                backend_owner=owner, backend_workspace_id=member.mount.workspace_id,
                processes={"terminal_manager": terminal} if index == 0 else {},
            )
            if self.bad_fields:
                runtime.service_urls.pop("browser_manager")
            runtimes[member.workspace_id] = runtime
            owner.unpublished_runtimes[member.workspace_id] = runtime
        return replace(owner.contract, members=tuple(members), backend_url=f"http://127.0.0.1:{port}"), runtimes


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
    monkeypatch.setattr(bootstrap, "resolve_backend_workspace_id", lambda _project, root: case.identities[root])
    monkeypatch.setattr(bootstrap, "start_managed_backend_runtime", case.start)
    return case


@pytest.fixture
def persisted_targets(startup_case: StartupCase, managed_backend_factory):
    case = startup_case
    registry = GatewayWorkspaceRegistry(storage_path=case.gateway_root / "workspaces.json")
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
                workspace_id=build_managed_local_workspace_id(str(root)), name=name,
                root_path=str(root), backend_url="http://127.0.0.1:41000",
                connection_kind="local", backend_workspace_id=case.identities[root], managed=True,
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
    persisted.activate(running[1].workspace_id)
    registry = await bootstrap.create_registry()
    assert len(startup_case.started) == 1
    assert registry.has_runtime(defaults[0].workspace_id)
    assert all(not registry.has_runtime(target.workspace_id) for target in running)
    await bootstrap._restore_managed_local_runtimes(
        registry=registry, default_workspace_id=defaults[0].workspace_id, gateway_root=startup_case.gateway_root,
    )
    assert len(startup_case.started) == 2
    restored_owner = registry.backend_owner(running[0].workspace_id)
    assert restored_owner is registry.backend_owner(running[1].workspace_id)
    assert all(registry.managed_runtime(target.workspace_id).backend_owner is restored_owner for target in running)
    assert registry.active_workspace_id == running[1].workspace_id
    assert not registry.has_runtime(stopped[0].workspace_id)
    assert registry.resolve(stopped[0].workspace_id).desired_running is False
    registry.close()


@pytest.mark.asyncio
async def test_gateway_restore_failure_keeps_intent_and_restores_other_targets(startup_case, persisted_targets, caplog) -> None:
    registry, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    missing, _ = add(("Missing-a", "Missing-b"), missing=True)
    healthy, _ = add(("Healthy",))
    registry.activate(missing[0].workspace_id)
    await bootstrap._restore_managed_local_runtimes(
        registry=registry, default_workspace_id=defaults[0].workspace_id, gateway_root=startup_case.gateway_root,
    )
    assert all(registry.resolve(target.workspace_id).desired_running for target in missing)
    assert all("工作区目录不存在" in registry.resolve(target.workspace_id).connection_error for target in missing)
    assert registry.has_runtime(healthy[0].workspace_id)
    assert registry.active_workspace_id == defaults[0].workspace_id
    assert any("已回退到默认工作区" in record.getMessage() for record in caplog.records)
    registry.close()


@pytest.mark.asyncio
async def test_gateway_restore_rejects_missing_explicit_owner_without_guessing_url(startup_case, persisted_targets, caplog) -> None:
    registry, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    orphan, _ = add(("Orphan",), owner=False)
    registry.activate(orphan[0].workspace_id)
    await bootstrap._restore_managed_local_runtimes(
        registry=registry, default_workspace_id=defaults[0].workspace_id, gateway_root=startup_case.gateway_root,
    )
    assert not startup_case.started
    assert "缺少显式 Backend owner 完整合同" in registry.resolve(orphan[0].workspace_id).connection_error
    assert registry.backend_owner(orphan[0].workspace_id) is None
    assert registry.active_workspace_id == defaults[0].workspace_id
    assert any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_startup_fallback_from_stopped_managed_workspace_logs_warning(startup_case, persisted_targets, caplog) -> None:
    persisted, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    stopped, _ = add(("Stopped",), desired_running=False)
    persisted.activate(stopped[0].workspace_id)
    registry = await bootstrap.create_registry()
    assert registry.active_workspace_id == defaults[0].workspace_id
    assert any(record.levelname == "WARNING" and "已回退到默认工作区" in record.getMessage() and stopped[0].workspace_id in record.getMessage() for record in caplog.records)
    registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("preserve_existing_managed_runtimes", [False, True])
async def test_create_registry_closes_runtime_when_later_startup_fails(startup_case, monkeypatch, cleanup_fails, preserve_existing_managed_runtimes) -> None:
    startup_case.backend_adopted = True
    startup_error = RuntimeError("configured remote Gateway reconciliation failed")
    if cleanup_fails:
        startup_case.auxiliary_failure = RuntimeError("owned workspace runtime close failed")
    async def fail_reconciliation(**_):
        raise startup_error
    monkeypatch.setattr(bootstrap, "reconcile_configured_remote_gateways", fail_reconciliation)
    if cleanup_fails:
        with pytest.raises(BaseExceptionGroup) as raised:
            await bootstrap.create_registry(preserve_existing_managed_runtimes=preserve_existing_managed_runtimes)
        assert raised.value.exceptions[0] is startup_error
        assert "owned workspace runtime close failed" in str(raised.value.exceptions[1])
    else:
        with pytest.raises(RuntimeError) as raised:
            await bootstrap.create_registry(preserve_existing_managed_runtimes=preserve_existing_managed_runtimes)
        assert raised.value is startup_error
    backend, terminal = startup_case.handles[0]
    assert terminal.events.count("close:terminal") == 1 and not terminal.detached
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
    with pytest.raises(RuntimeError) as raised:
        await bootstrap.create_registry(preserve_existing_managed_runtimes=True)
    if failure_point == "registry_publish":
        assert raised.value is error
    else:
        assert "服务地址" in str(raised.value)
    backend, terminal = startup_case.handles[0]
    assert backend.detached and not backend.closed
    assert terminal.closed and not terminal.detached
    assert terminal.events.count("close:terminal") == 1


@pytest.mark.asyncio
async def test_default_external_backend_discards_only_stopped_singleton_owner(startup_case, persisted_targets, monkeypatch) -> None:
    _, add = persisted_targets
    defaults, _ = add(("Default",), default=True)
    monkeypatch.setenv("BOXTEAM_DEFAULT_BACKEND_URL", "http://127.0.0.1:41000")
    async def healthy(*_, **__):
        return {"workspace_id": startup_case.identities[startup_case.default_root]}
    monkeypatch.setattr(bootstrap, "wait_for_workspace_health", healthy)
    registry = await bootstrap.create_registry()
    assert registry.backend_owner(defaults[0].workspace_id) is None
    assert registry.resolve(defaults[0].workspace_id).managed is False
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
                gateway_root=startup_case.gateway_root,
            )
        assert raised.value is failure
        assert graph.owner.contract is not old_contract
        assert graph.owner.error is None
        assert graph.owner.process is startup_case.handles[0][0]
        assert not graph.owner.process.closed
        assert not graph.owner.unpublished_runtimes
        assert registry.active_workspace_id == running[1].workspace_id
        assert all(registry.resolve(target.workspace_id).connection_error is None for target in running)
        persisted = GatewayWorkspaceRegistry(storage_path=registry._storage_path)
        assert persisted.backend_owner(running[0].workspace_id).contract == graph.owner.contract
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
            await bootstrap._complete_registry_startup(
                gateway_root=startup_case.gateway_root,
                resolved_gateway_config=GatewayConfig(), registry=registry,
                persisted_targets_by_id={target.workspace_id: target},
                persisted_default=target, persisted_active_workspace_id=target.workspace_id,
                default_root_path=startup_case.default_root, root_path=target.root_path,
                default_workspace_id=target.workspace_id,
                preserve_existing_managed_runtimes=False,
            )
        assert raised.value is failure
        backend, terminal = startup_case.handles[0]
        assert graph.owner.process is backend and not backend.closed
        assert not terminal.closed
        assert not graph.owner.unpublished_runtimes
        assert registry.managed_runtime(target.workspace_id).backend_owner is graph.owner
    finally:
        registry.remove_commit_observer(observer)
        registry.close()
    assert backend.events.count("close:backend") == 1
    assert terminal.events.count("close:terminal") == 1
