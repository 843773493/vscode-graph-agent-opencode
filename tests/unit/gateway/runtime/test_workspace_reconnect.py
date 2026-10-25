"""完整 Backend owner 的生命周期、proof 与一次接管边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import app.gateway.runtime.controller as runtime_controller_module
from app.core.mounted_workspaces import (
    MountedWorkspace,
    mounted_workspace_manifest_digest,
)
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import (
    GatewayAuthContext,
    verify_federation_token,
    verify_gateway_token,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import (
    _shared,
    federation,
    workspaces_lifecycle,
    workspaces_managed,
)
from app.gateway.runtime import local_workspace
from app.gateway.runtime.controller import (
    GatewayWorkspaceRuntimeController,
    reconnect_gateway_workspace,
)
from app.gateway.runtime.local_workspace import (
    _adopt_local_node_service,
    _adopt_workspace_backend,
    rebuild_managed_backend_runtime,
    start_managed_backend_runtime,
    verify_managed_backend_group,
    wait_for_workspace_health,
    workspace_config_proof_expectation,
)
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.port_forwarding import get_port_forward_manager
from tests.support.gateway_managed_backends import ManagedBackendGraph, ProcessProbe

UUID_A = "00000000-0000-4000-8000-000000000001"
UUID_B = "00000000-0000-4000-8000-000000000002"


@dataclass
class ProcessBoundary:
    identities: dict[Path, str] = field(default_factory=dict)
    proofs: dict[str, tuple[tuple[MountedWorkspace, ...], str, int]] = field(default_factory=dict)
    config_proofs: dict[str, dict[str, object]] = field(default_factory=dict)
    starts: list[dict[str, object]] = field(default_factory=list)
    handles: list[ProcessProbe] = field(default_factory=list)
    backend_outcomes: list[Exception | None] = field(default_factory=list)
    failed_urls: dict[str, Exception] = field(default_factory=dict)
    nodes: list[tuple[str, ProcessProbe]] = field(default_factory=list)
    next_port: int = 43000

    def register(self, graph: ManagedBackendGraph, registry: GatewayWorkspaceRegistry) -> None:
        mounts = registry.resolve_backend_mounts(graph.owner.contract)
        self.identities.update({mount.workspace_root: mount.workspace_id for mount in mounts})
        if graph.owner.contract.backend_url:
            self.proofs[graph.owner.contract.backend_url] = (mounts, graph.owner.contract.generation, 7000)

    def allocate(self) -> int:
        self.next_port += 1
        return self.next_port

    def start_backend(self, **options: object) -> ProcessProbe:
        self.starts.append(options)
        handle = ProcessProbe(name=f"backend-{len(self.starts)}")
        self.handles.append(handle)
        url = f"http://127.0.0.1:{options['port']}"
        self.proofs[url] = (options["mounts"], str(options["backend_generation"]), 7000 + len(self.starts))
        if self.backend_outcomes and (error := self.backend_outcomes.pop(0)) is not None:
            self.failed_urls[url] = error
        else:
            self.failed_urls.pop(url, None)
        return handle

    def start_node(self, **options: object) -> ProcessProbe:
        handle = ProcessProbe(name=str(options["service"]))
        self.nodes.append((str(options["service"]), handle))
        return handle

    async def health(self, url: str, workspace_id: str, *_: object, **__: object) -> dict[str, object]:
        if "/api/v1/health" not in url:
            return {"workspace_id": workspace_id, "process_id": 8000}
        backend_url = url.removesuffix("/api/v1/health")
        if backend_url in self.failed_urls:
            raise self.failed_urls[backend_url]
        mounts, generation, pid = self.proofs[backend_url]
        assert workspace_id in {mount.workspace_id for mount in mounts}
        result = {
            "workspace_id": workspace_id, "status": "ok", "process_id": pid,
            "backend_generation": generation, "mount_manifest_digest": mounted_workspace_manifest_digest(mounts),
        }
        if workspace_id in self.config_proofs:
            result["config_proof"] = self.config_proofs[workspace_id]
        return result


@dataclass
class WorkspaceRuntimeControllerCase:
    registry: GatewayWorkspaceRegistry
    graph: ManagedBackendGraph
    controller: GatewayWorkspaceRuntimeController
    runtime_actions: list[tuple[str, str]]
    lifecycle_states: dict[str, str]
    blockers_by_workspace: dict[str, list[dict[str, object]]]


@pytest.fixture
def process_boundary(monkeypatch: pytest.MonkeyPatch) -> ProcessBoundary:
    boundary = ProcessBoundary()
    monkeypatch.setattr(local_workspace, "resolve_backend_workspace_id", lambda _project, root, **_: boundary.identities[root.resolve()])
    monkeypatch.setattr("app.gateway.runtime.group_actions.resolve_backend_workspace_id", lambda _project, root, **_: boundary.identities[root.resolve()])
    monkeypatch.setattr(local_workspace, "allocate_local_port", boundary.allocate)
    monkeypatch.setattr(local_workspace, "is_local_port_available", lambda _port: True)
    monkeypatch.setattr(local_workspace, "start_local_backend_process", boundary.start_backend)
    monkeypatch.setattr(local_workspace, "start_local_node_service_process", boundary.start_node)
    monkeypatch.setattr(local_workspace, "wait_for_workspace_health", boundary.health)

    async def no_adoption(**_: object) -> None:
        return None

    monkeypatch.setattr(local_workspace, "_adopt_workspace_backend", no_adoption)
    monkeypatch.setattr(local_workspace, "_adopt_local_node_service", no_adoption)
    return boundary


@pytest.fixture
def single_workspace_graph(tmp_path: Path, managed_backend_factory, process_boundary: ProcessBoundary) -> ManagedBackendGraph:
    target = WorkspaceTarget(
        workspace_id="member-a", name="A", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        backend_workspace_id=UUID_A, managed=True,
    )
    graph = managed_backend_factory(
        (target,), backend_url=target.backend_url, process=ProcessProbe("old"),
        auxiliary_processes={"member-a": {"terminal_manager": ProcessProbe("terminal"), "browser_manager": ProcessProbe("browser")}},
    )
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "single-workspace-registry.json"
    )
    registry.upsert(target)
    graph.publish(registry)
    process_boundary.register(graph, registry)
    return graph


@pytest.fixture
def single_workspace_registry(tmp_path: Path, single_workspace_graph: ManagedBackendGraph):
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "runtime-registry.json")
    registry.upsert(single_workspace_graph.targets[0])
    single_workspace_graph.publish(registry)
    return registry


@pytest.fixture
def controller_registry_store(tmp_path: Path, request: pytest.FixtureRequest):
    if getattr(request, "param", False):
        with GatewayStateStore(path=tmp_path / "stopped-controller.sqlite") as state:
            yield state
    else:
        yield None


@pytest.fixture
def workspace_runtime_controller_case(tmp_path: Path, managed_backend_factory, process_boundary: ProcessBoundary, monkeypatch: pytest.MonkeyPatch, controller_registry_store) -> Iterator[WorkspaceRuntimeControllerCase]:
    targets = []
    for name, identity in (("a", UUID_A), ("b", UUID_B)):
        root = tmp_path / name
        root.mkdir()
        targets.append(WorkspaceTarget(
            workspace_id=f"member-{name}", name=name, root_path=str(root),
            backend_url="http://127.0.0.1:41000", connection_kind="local",
            backend_workspace_id=identity, managed=True,
        ))
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json", state_store=controller_registry_store)
    for target in targets:
        registry.upsert(target, activate=False)
    graph = managed_backend_factory(
        tuple(targets), backend_url=targets[0].backend_url, process=ProcessProbe("old"),
        auxiliary_processes={target.workspace_id: {"terminal_manager": ProcessProbe("terminal"), "browser_manager": ProcessProbe("browser")} for target in targets},
    )
    graph.publish(registry)
    process_boundary.register(graph, registry)
    controller = GatewayWorkspaceRuntimeController(
        registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs",
        drain_timeout_seconds=0, drain_poll_interval_seconds=0.001,
    )
    calls: list[tuple[str, str]] = []
    states = {target.workspace_id: "ready" for target in targets}
    blockers: dict[str, list[dict[str, object]]] = {target.workspace_id: [] for target in targets}

    async def runtime_action(target, path: str, **_: object) -> dict[str, object]:
        calls.append((target.workspace_id, path))
        states[target.workspace_id] = "draining" if path.endswith("/drain") else "stopping" if path.endswith("/force") else "ready"
        return {}

    async def runtime_status(target, **_: object) -> dict[str, object]:
        state = states[target.workspace_id]
        return {"lifecycle_state": state, "accepting_jobs": state == "ready", "blockers": blockers[target.workspace_id]}

    async def pending(*_: object, **__: object) -> None:
        return None

    async def listed() -> list[object]:
        return []

    monkeypatch.setattr(controller, "_runtime_action", runtime_action)
    monkeypatch.setattr(controller, "_runtime_status", runtime_status)
    monkeypatch.setattr(controller, "_pending_startup_contract", pending)
    monkeypatch.setattr(registry, "list_dtos", listed)
    yield WorkspaceRuntimeControllerCase(
        registry=registry,
        graph=graph,
        controller=controller,
        runtime_actions=calls,
        lifecycle_states=states,
        blockers_by_workspace=blockers,
    )
    registry.close()


@pytest.fixture
def local_http(monkeypatch: pytest.MonkeyPatch):
    client_type = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(local_workspace.httpx, "AsyncClient", lambda *, timeout, trust_env: client_type(
            timeout=timeout, transport=httpx.MockTransport(handler), trust_env=trust_env,
        ))
    return install


@pytest.mark.asyncio
async def test_workspace_health_rejects_uuid_mismatch_without_retry(local_http) -> None:
    calls = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-BoxTeam-Workspace-Id"] == UUID_A
        return httpx.Response(200, json={"workspace_id": UUID_B})
    local_http(response)
    with pytest.raises(RuntimeError, match="backend UUID 不匹配"):
        await wait_for_workspace_health("http://127.0.0.1:41000/api/v1/health", UUID_A)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reconnect_managed_local_workspace_keeps_stable_id(tmp_path: Path, single_workspace_graph, process_boundary, monkeypatch) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    member = single_workspace_graph.owner.contract.members[0]
    target = WorkspaceTarget(
        workspace_id=member.workspace_id, name="Managed", root_path=str(tmp_path),
        backend_url=single_workspace_graph.owner.contract.backend_url, backend_workspace_id=UUID_A,
        connection_kind="local", managed=True, connection_error="旧连接失败",
    )
    registry.upsert(target)
    single_workspace_graph.publish(registry)
    single_workspace_graph.owner.close()
    async def listed():
        return []
    monkeypatch.setattr(registry, "list_dtos", listed)
    await reconnect_gateway_workspace(
        registry=registry, workspace_id=target.workspace_id, project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    assert registry.resolve(target.workspace_id).connection_error is None
    assert registry.backend_owner(target.workspace_id) is single_workspace_graph.owner
    assert registry.managed_runtime(target.workspace_id).backend_owner is single_workspace_graph.owner
    assert len(process_boundary.starts) == 1
    registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize(
    "cancel_reconcile",
    [False, True],
    ids=["secondary-error", "reconcile-cancelled"],
)
async def test_reconnect_http_keeps_published_projection_and_reports_both_errors(
    workspace_runtime_controller_case,
    deletion_http_application,
    monkeypatch: pytest.MonkeyPatch,
    cancel_reconcile: bool,
) -> None:
    registry = workspace_runtime_controller_case.registry
    controller = workspace_runtime_controller_case.controller

    class _TrackingRuntime(WorkspaceRuntime):
        def __init__(self, url: str) -> None:
            super().__init__(service_urls={"workspace_api": url})
            self.closed = False

        def close(self) -> None:
            self.closed = True

    connection = RemoteGatewayConnection(
        connection_id="rgw_reconnect_http",
        name="Reconnect HTTP",
        host="reconnect.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="reconnect",
        remote_gateway_port=8014,
        remote_gateway_id="reconnect-gateway",
        protocol_version=1,
        source_owner="manual",
    )
    workspace_id = "reconnect-http-workspace"
    old_runtime = _TrackingRuntime("http://127.0.0.1:41001")
    new_runtime = _TrackingRuntime("http://127.0.0.1:42001")
    old_target = WorkspaceTarget(
        workspace_id=workspace_id,
        name="Reconnect HTTP workspace",
        root_path=None,
        backend_url=old_runtime.service_urls["workspace_api"],
        connection_kind="remote_gateway",
        backend_workspace_id=UUID_B,
        owner="remote_projection",
        connection_id=connection.connection_id,
        remote_gateway_connection_id=connection.connection_id,
        remote_workspace_id="remote-workspace",
        remote_service_names=("workspace_api",),
    )
    registry.apply_remote_projection_snapshot(
        connection=connection,
        projections=(old_target,),
        runtime=old_runtime,
    )
    previous_revision = registry.registry_revision
    state = registry._state_store
    assert state is not None
    primary_error = RuntimeError("observer failed after remote projection commit")

    async def reconnect_remote_gateway(**_: object) -> None:
        registry.apply_remote_projection_snapshot(
            connection=connection,
            projections=(
                replace(
                    old_target,
                    backend_url=new_runtime.service_urls["workspace_api"],
                ),
            ),
            runtime=new_runtime,
        )

    def fail_commit_observer() -> None:
        raise primary_error

    reconcile_calls = 0

    async def fail_dependent_reconcile() -> None:
        nonlocal reconcile_calls
        reconcile_calls += 1
        if cancel_reconcile:
            raise asyncio.CancelledError("dependent reconcile cancelled")
        raise OSError("port-forward reconcile failed after commit")

    monkeypatch.setattr(
        runtime_controller_module,
        "reconnect_remote_gateway",
        reconnect_remote_gateway,
    )
    monkeypatch.setattr(controller, "_on_registry_reconciled", fail_dependent_reconcile)
    registry.add_commit_observer(fail_commit_observer)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=deletion_http_application),
            base_url="http://gateway",
        ) as client:
            if cancel_reconcile:
                with pytest.raises(
                    asyncio.CancelledError,
                    match="dependent reconcile cancelled",
                ) as cancelled:
                    await client.post(
                        f"/api/gateway/workspaces/{workspace_id}/reconnect",
                        headers={"X-Request-ID": "reconnect-postcommit-01"},
                    )
                assert cancelled.value.__context__ is primary_error
            else:
                response = await client.post(
                    f"/api/gateway/workspaces/{workspace_id}/reconnect",
                    headers={"X-Request-ID": "reconnect-postcommit-01"},
                )
                assert response.status_code == 502, response.text
                assert response.headers["X-Request-ID"] == "reconnect-postcommit-01"
                assert response.json() == {
                    "detail": (
                        "observer failed after remote projection commit\n"
                        "远程 Gateway 新 URL 已提交，但依赖资源对账失败: "
                        "port-forward reconcile failed after commit"
                    ),
                    "request_id": "reconnect-postcommit-01",
                }

        assert primary_error.args == (
            "observer failed after remote projection commit",
        )
        assert reconcile_calls == 1
        assert registry.resolve(workspace_id).backend_url == "http://127.0.0.1:42001"
        assert registry.resolve(workspace_id).connection_error is None
        assert registry.remote_gateway_url(connection.connection_id) == "http://127.0.0.1:42001"
        assert registry._remote_gateway_runtimes[connection.connection_id] is new_runtime
        assert registry.registry_revision == previous_revision + 1
        assert state.get_registry_revision() == registry.registry_revision
        saved = state.load_workspace_registry()
        assert saved is not None
        assert any(
            target["workspace_id"] == workspace_id
            and target["backend_url"] == "http://127.0.0.1:42001"
            for target in saved["targets"]
        )
        assert new_runtime.closed is False
        assert old_runtime.closed is True
        assert registry.remote_gateway_connection(connection.connection_id) == connection
    finally:
        registry.remove_commit_observer(fail_commit_observer)


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_reconnect_http_preserves_primary_error_when_connection_error_recording_fails(
    workspace_runtime_controller_case,
    deletion_http_application,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = workspace_runtime_controller_case.registry
    controller = workspace_runtime_controller_case.controller
    target = WorkspaceTarget(
        workspace_id="external-reconnect-error",
        name="External workspace",
        root_path="/external/reconnect-error",
        backend_url="http://127.0.0.1:43001",
        connection_kind="local",
        backend_workspace_id=UUID_B,
        managed=False,
    )
    registry.upsert(
        target,
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": target.backend_url},
            backend_workspace_id=UUID_B,
        ),
        activate=False,
    )
    primary_error = OSError("reconnect failed before registry commit")
    secondary_error = RuntimeError("connection_error persistence failed")
    calls = 0

    async def fail_reconnect(_workspace_id: str) -> None:
        raise primary_error

    def fail_mark_connection_error(_workspace_id: str, _error: str) -> None:
        nonlocal calls
        calls += 1
        raise secondary_error

    monkeypatch.setattr(controller, "reconnect_ssh", fail_reconnect)
    monkeypatch.setattr(registry, "mark_connection_error", fail_mark_connection_error)
    previous_revision = registry.registry_revision
    previous_commit = registry._last_committed_snapshot
    state = registry._state_store
    assert state is not None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=deletion_http_application),
        base_url="http://gateway",
    ) as client:
        response = await client.post(
            f"/api/gateway/workspaces/{target.workspace_id}/reconnect",
            headers={"X-Request-ID": "reconnect-mark-error-secondary-01"},
        )

    assert response.status_code == 502, response.text
    assert response.headers["X-Request-ID"] == "reconnect-mark-error-secondary-01"
    assert response.json() == {
        "detail": (
            "reconnect failed before registry commit\n"
            "记录 connection_error 失败: connection_error persistence failed"
        ),
        "request_id": "reconnect-mark-error-secondary-01",
    }
    assert primary_error.args == ("reconnect failed before registry commit",)
    assert calls == 1
    assert registry.registry_revision == previous_revision
    assert registry._last_committed_snapshot is previous_commit
    assert state.get_registry_revision() == previous_revision
    assert registry.resolve(target.workspace_id).connection_error is None


@pytest.mark.asyncio
async def test_runtime_controller_rejects_restart_for_external_backend(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(WorkspaceTarget(
        workspace_id="external", name="External", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        backend_workspace_id=UUID_A, managed=False,
    ))
    controller = GatewayWorkspaceRuntimeController(registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs")
    with pytest.raises(ValueError, match="Gateway 托管"):
        await controller.safe_restart_managed_backend("external", request_id="req")


def test_runtime_controller_updates_health_controller_config(tmp_path: Path) -> None:
    controller = GatewayWorkspaceRuntimeController(
        registry=GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json"),
        project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    controller.update_health_controller_config(request_timeout_seconds=4, poll_interval_seconds=1.5)
    assert controller._health_request_timeout_seconds == 4
    assert controller._health_poll_interval_seconds == 1.5
    with pytest.raises(ValueError, match="request_timeout_seconds"):
        controller.update_health_controller_config(request_timeout_seconds=0, poll_interval_seconds=1)


@pytest.mark.asyncio
async def test_runtime_controller_starts_and_stops_optional_workspace(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    stopped = await controller.stop_managed_backend("member-a", request_id="stop", group_request=graph.group_request)
    assert stopped.status == "stopped"
    assert not any(registry.has_runtime(member_id) for member_id in graph.owner.contract.workspace_ids)
    assert graph.owner.process is None
    started = await controller.start_managed_backend("member-b", request_id="start", group_request=graph.group_request)
    assert started.status == "started"
    assert len(process_boundary.starts) == 1
    assert all(registry.has_runtime(member_id) for member_id in graph.owner.contract.workspace_ids)
    registry.resolve("member-a").system_default = True
    with pytest.raises(PermissionError, match="默认工作区不能关闭"):
        await controller.stop_managed_backend("member-b", request_id="default", group_request=graph.group_request)


@pytest.mark.asyncio
async def test_runtime_controller_blocks_stop_for_gateway_proxy_stream(workspace_runtime_controller_case) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    lifecycle_states = workspace_runtime_controller_case.lifecycle_states
    registry.acquire_route_reference("member-b", streaming=True)
    try:
        blocked = await controller.stop_managed_backend("member-a", request_id="stream", group_request=graph.group_request)
        assert blocked.status == "blocked"
        assert [(item.workspace_id, item.kind) for item in blocked.blockers] == [("member-b", "proxy_stream")]
        assert all(state == "ready" for state in lifecycle_states.values())
        assert sum(path.endswith("/cancel") for _, path in runtime_actions) == 2
        assert graph.owner.process is not None
    finally:
        registry.release_route_reference("member-b", streaming=True)


@pytest.mark.asyncio
async def test_shared_safe_restart_blocks_and_force_interrupts_every_member(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    lifecycle_states = workspace_runtime_controller_case.lifecycle_states
    blockers_by_workspace = workspace_runtime_controller_case.blockers_by_workspace
    blockers_by_workspace["member-b"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    blocked = await controller.safe_restart_managed_backend("member-a", request_id="safe", group_request=graph.group_request)
    assert blocked.status == "blocked" and blocked.blockers[0].workspace_id == "member-b"
    assert not process_boundary.starts
    forced = await controller.force_restart_managed_backend("member-a", request_id="force", group_request=graph.group_request)
    assert forced.status == "restarted" and forced.forced
    assert {member_id for member_id, path in runtime_actions if path.endswith("/force")} == {"member-a", "member-b"}
    assert len(process_boundary.starts) == 1
    assert all(registry.managed_runtime(member_id).backend_owner is graph.owner for member_id in lifecycle_states)


@pytest.mark.asyncio
async def test_runtime_controller_serializes_managed_restarts(workspace_runtime_controller_case, process_boundary, monkeypatch) -> None:
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    active = peak = 0
    rebuild = rebuild_managed_backend_runtime

    async def observe(**options):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return await rebuild(**options)
        finally:
            active -= 1
    monkeypatch.setattr("app.gateway.runtime.group_actions.rebuild_managed_backend_runtime", observe)
    async def restart(member_id):
        await controller.safe_restart_managed_backend(member_id, request_id=member_id, group_request=graph.group_request)
    # 后一个完整合同在锁前已经取到；单成员兼容调用只适用于单工作区图。
    first = asyncio.create_task(restart("member-a"))
    await asyncio.sleep(0.002)
    second = asyncio.create_task(restart("member-b"))
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert peak == 1
    assert sum(isinstance(result, ValueError) for result in outcomes) == 1
    assert len(process_boundary.starts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_fails", [False, True])
async def test_runtime_controller_records_restart_failure_after_old_runtime_recovery(
    workspace_runtime_controller_case, process_boundary, monkeypatch, recovery_fails: bool,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    recorded = {}
    startup = {"candidate_ref": "candidate", "target_generation": "config-new", "fencing_token": "secret"}
    async def pending(*_, **__):
        return startup
    async def record(_target, **options):
        recorded.update(options)
    monkeypatch.setattr(controller, "_pending_startup_contract", pending)
    monkeypatch.setattr(controller, "_record_pending_restart_failure", record)
    process_boundary.backend_outcomes[:] = [RuntimeError("候选启动失败"), RuntimeError("旧组恢复失败") if recovery_fails else None]
    old = graph.owner.contract
    with pytest.raises(RuntimeError, match="候选启动失败"):
        await controller.safe_restart_managed_backend("member-a", request_id="recovery", group_request=graph.group_request)
    assert recorded["old_runtime_recovered"] is not recovery_fails
    assert recorded["candidate_ref"] == "candidate"
    assert graph.owner.contract == old
    assert all((registry.resolve(member_id).connection_error is not None) is recovery_fails for member_id in old.workspace_ids)


@pytest.mark.asyncio
async def test_runtime_controller_resolves_pending_after_candidate_restart(tmp_path: Path, local_http) -> None:
    captured = {}
    def response(request: httpx.Request) -> httpx.Response:
        captured[request.method] = request
        if request.method == "GET":
            return httpx.Response(200, json={"workspace_id": UUID_A, "config_proof": {"loaded_source": "pending", "config_domain": "workspace"}})
        return httpx.Response(200, json={})
    local_http(response)
    controller = GatewayWorkspaceRuntimeController(
        registry=GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json"),
        project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    await controller._resolve_pending_restart(WorkspaceTarget(
        workspace_id="candidate", name="Candidate", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:42000", connection_kind="local", backend_workspace_id=UUID_A,
    ), candidate_ref="candidate-ref", request_id="request-id")
    assert captured["GET"].headers["X-BoxTeam-Workspace-Id"] == UUID_A
    assert captured["POST"].url.path == "/api/v1/config/pending/resolve"
    assert captured["POST"].url.params["candidate_ref"] == "candidate-ref"
    assert captured["POST"].headers["X-Request-ID"] == "request-id"


@pytest.mark.asyncio
async def test_managed_runtime_rejects_terminal_wrong_uuid_before_backend_and_closes_owned_handles(
    single_workspace_graph, process_boundary, monkeypatch, tmp_path, single_workspace_registry) -> None:
    single_workspace_graph.owner.close()
    runtime = single_workspace_graph.runtimes["member-a"]
    adopted = runtime.processes["terminal_manager"]
    runtime.processes.clear()
    async def adopt(**_):
        return adopted
    async def wrong_uuid(*_, **__):
        raise RuntimeError("backend UUID 不匹配")
    monkeypatch.setattr(local_workspace, "_adopt_local_node_service", adopt)
    monkeypatch.setattr(local_workspace, "wait_for_workspace_health", wrong_uuid)
    with pytest.raises(RuntimeError, match="backend UUID 不匹配"):
        await start_managed_backend_runtime(owner=single_workspace_graph.owner, registry=single_workspace_registry, project_root=tmp_path, log_dir=tmp_path / "logs")
    assert not process_boundary.starts and not process_boundary.nodes
    assert adopted.closed


@pytest.mark.asyncio
async def test_candidate_runtime_start_failure_detaches_adopted_and_closes_new_processes(single_workspace_graph, process_boundary, monkeypatch, tmp_path, single_workspace_registry) -> None:
    single_workspace_graph.owner.close()
    adopted = ProcessProbe("adopted-terminal")
    async def adopt(*, service_name, **_):
        return adopted if service_name == "terminal_manager" else None
    monkeypatch.setattr(local_workspace, "_adopt_local_node_service", adopt)
    error = RuntimeError("候选 health 失败")
    process_boundary.backend_outcomes[:] = [error]
    with pytest.raises(RuntimeError) as raised:
        await start_managed_backend_runtime(
            owner=single_workspace_graph.owner, registry=single_workspace_registry, project_root=tmp_path, log_dir=tmp_path / "logs",
            preserve_adopted_processes_on_failure=True,
        )
    assert raised.value is error
    assert adopted.detached and not adopted.closed
    assert process_boundary.handles[0].closed
    assert process_boundary.nodes[0][1].closed
    assert single_workspace_graph.owner.process is None


def test_workspace_config_proof_expectation_redacts_fencing_token() -> None:
    proof = workspace_config_proof_expectation({
        "candidate_id": "candidate-1", "pending_revision": 4, "candidate_digest": "candidate-digest",
        "effective_digest": "effective-digest", "target_generation": "generation-2",
        "fencing_token": "fence-secret", "secret_binding_digest": "binding-digest",
    })
    assert proof["loaded_commit_revision"] == 4 and proof["generation_id"] == "generation-2"
    assert proof["fencing_token_digest"] != "fence-secret" and "fencing_token" not in proof


@pytest.mark.asyncio
async def test_backend_restart_preserves_terminal_and_browser(single_workspace_graph, process_boundary, tmp_path, single_workspace_registry) -> None:
    owner = single_workspace_graph.owner
    old_handle = owner.process
    runtime = single_workspace_graph.runtimes["member-a"]
    auxiliary_handles = dict(runtime.processes)
    contract, runtimes = await rebuild_managed_backend_runtime(
        owner=owner, registry=single_workspace_registry, desired_members=owner.contract.members, runtimes=single_workspace_graph.runtimes,
        project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    assert old_handle.closed and owner.process is process_boundary.handles[0]
    assert runtime.processes == auxiliary_handles and all(not handle.closed for handle in auxiliary_handles.values())
    assert runtimes["member-a"] is runtime
    assert contract.generation != owner.contract.generation
    assert "workspace_api" not in runtime.processes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["health", "config-proof"])
async def test_backend_restart_failure_keeps_old_generation(single_workspace_graph, process_boundary, tmp_path, failure, single_workspace_registry) -> None:
    old_contract = single_workspace_graph.owner.contract
    old_handle = single_workspace_graph.owner.process
    startup = {
        "candidate_ref": "candidate-ref", "candidate_id": "candidate", "pending_revision": 2,
        "candidate_digest": "candidate-digest", "effective_digest": "effective-digest",
        "target_generation": "config-generation", "fencing_token": "fence", "secret_binding_digest": "bindings",
    }
    if failure == "health":
        process_boundary.backend_outcomes[:] = [RuntimeError("candidate backend unhealthy"), None]
    else:
        process_boundary.config_proofs[UUID_A] = {"loaded_source": "active"}
    with pytest.raises(RuntimeError, match="candidate backend unhealthy|config proof 不匹配"):
        await rebuild_managed_backend_runtime(
            owner=single_workspace_graph.owner, registry=single_workspace_registry, desired_members=old_contract.members, runtimes=single_workspace_graph.runtimes,
            project_root=tmp_path, log_dir=tmp_path / "logs",
            startup_contracts={"member-a": startup} if failure == "config-proof" else None,
        )
    assert old_handle.closed and process_boundary.handles[0].closed
    assert single_workspace_graph.owner.process is None
    restored_contract, _ = await start_managed_backend_runtime(
        owner=single_workspace_graph.owner, registry=single_workspace_registry, mount_contract=old_contract, reusable_runtimes=single_workspace_graph.runtimes,
        project_root=tmp_path, log_dir=tmp_path / "logs",
        preferred_backend_port=41000, preserve_generation=True,
    )
    assert restored_contract == old_contract
    assert single_workspace_graph.owner.process is process_boundary.handles[1]
    assert single_workspace_graph.owner.contract == old_contract
    assert single_workspace_graph.runtimes["member-a"].service_urls["workspace_api"] == old_contract.backend_url


@pytest.mark.asyncio
@pytest.mark.parametrize("service,port", [("terminal_manager", 42001), ("browser_manager", 42002)])
async def test_gateway_adopts_matching_node_manager(local_http, service, port) -> None:
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"workspace_id": UUID_A, "process_id": 43210})
    local_http(response)
    adopted = await _adopt_local_node_service(service_url=f"http://127.0.0.1:{port}", service_name=service, backend_workspace_id=UUID_A)
    assert adopted.pid == 43210
    assert requests[0].url.path == "/health"


@pytest.mark.asyncio
async def test_gateway_rejects_persisted_browser_manager_url_without_port() -> None:
    with pytest.raises(ValueError, match="缺少端口"):
        await _adopt_local_node_service(service_url="http://127.0.0.1", service_name="browser_manager", backend_workspace_id=UUID_A)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["terminal_manager", "browser_manager"])
@pytest.mark.parametrize("identity", [UUID_B, None, "not-a-uuid"])
async def test_gateway_rejects_mismatched_node_manager_without_adoption(local_http, service, identity) -> None:
    local_http(lambda _: httpx.Response(200, json={"workspace_id": identity, "process_id": 43211}))
    with pytest.raises(RuntimeError, match="backend UUID 不匹配"):
        await _adopt_local_node_service(service_url="http://127.0.0.1:42001", service_name=service, backend_workspace_id=UUID_A)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "generation", "manifest", "pid"])
async def test_gateway_adopts_matching_workspace_backend(workspace_runtime_controller_case, local_http, mismatch) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    contract = graph.owner.contract
    requests = []
    def response(request):
        identity = request.headers["X-BoxTeam-Workspace-Id"]
        requests.append(identity)
        payload = {"workspace_id": identity, "status": "ok", "process_id": 43212,
                   "backend_generation": contract.generation, "mount_manifest_digest": mounted_workspace_manifest_digest(registry.resolve_backend_mounts(contract))}
        if mismatch == "generation":
            payload["backend_generation"] = "stale"
        elif mismatch == "manifest":
            payload["mount_manifest_digest"] = "wrong"
        elif mismatch == "pid" and identity == UUID_B:
            payload["process_id"] = 43213
        return httpx.Response(200, json=payload)
    local_http(response)
    if mismatch is None:
        adopted = await _adopt_workspace_backend(contract=contract, mounts=registry.resolve_backend_mounts(contract))
        assert adopted.pid == 43212 and requests == [UUID_A, UUID_B]
    else:
        with pytest.raises(RuntimeError, match="完整组"):
            await _adopt_workspace_backend(contract=contract, mounts=registry.resolve_backend_mounts(contract))


@pytest.mark.asyncio
@pytest.mark.parametrize("adopt_backend", [False, True])
async def test_managed_runtime_adopts_or_reclaims_persisted_backend_once(single_workspace_graph, process_boundary, monkeypatch, tmp_path, adopt_backend, single_workspace_registry) -> None:
    single_workspace_graph.owner.close()
    previous = single_workspace_graph.owner.contract
    backend = ProcessProbe("persisted-backend")
    auxiliaries = {service: ProcessProbe(service) for service in ("terminal_manager", "browser_manager")}
    async def adopt_backend_handle(**_):
        return backend
    async def adopt_node(*, service_name, **_):
        return auxiliaries[service_name]
    monkeypatch.setattr(local_workspace, "_adopt_workspace_backend", adopt_backend_handle)
    monkeypatch.setattr(local_workspace, "_adopt_local_node_service", adopt_node)
    contract, runtimes = await start_managed_backend_runtime(
        owner=single_workspace_graph.owner, registry=single_workspace_registry, project_root=tmp_path, log_dir=tmp_path / "logs", adopt_existing_backend=adopt_backend,
    )
    assert contract.workspace_ids == ("member-a",)
    assert runtimes["member-a"].processes == auxiliaries
    assert not process_boundary.nodes
    assert len(process_boundary.starts) == (0 if adopt_backend else 1)
    assert backend.closed is not adopt_backend
    assert single_workspace_graph.owner.adopted is adopt_backend
    if adopt_backend:
        assert single_workspace_graph.owner.contract == previous


@pytest.mark.asyncio
async def test_managed_runtime_replaces_default_backend_when_adoption_is_disabled(single_workspace_graph, process_boundary, monkeypatch, tmp_path, single_workspace_registry) -> None:
    single_workspace_graph.owner.close()
    stale = ProcessProbe("stale")
    async def adopt(**_):
        return stale
    monkeypatch.setattr(local_workspace, "_adopt_workspace_backend", adopt)
    await start_managed_backend_runtime(owner=single_workspace_graph.owner, registry=single_workspace_registry, project_root=tmp_path, log_dir=tmp_path / "logs",
                                        preferred_backend_port=8010, adopt_existing_backend=False)
    assert stale.closed and process_boundary.starts[0]["port"] == 8010
    assert [service for service, _ in process_boundary.nodes] == ["terminal", "browser"]


@pytest.mark.asyncio
async def test_managed_runtime_reuses_browser_without_starting_replacement(single_workspace_graph, process_boundary, monkeypatch, tmp_path, single_workspace_registry) -> None:
    single_workspace_graph.owner.close()
    adopted = ProcessProbe("browser")
    async def adopt(*, service_name, **_):
        return adopted if service_name == "browser_manager" else None
    monkeypatch.setattr(local_workspace, "_adopt_local_node_service", adopt)
    _, runtimes = await start_managed_backend_runtime(owner=single_workspace_graph.owner, registry=single_workspace_registry, project_root=tmp_path, log_dir=tmp_path / "logs")
    assert [service for service, _ in process_boundary.nodes] == ["terminal"]
    assert process_boundary.starts[0]["mounts"][0].browser_backend_url == runtimes["member-a"].service_urls["browser_manager"]
    assert runtimes["member-a"].processes["browser_manager"] is adopted


@pytest.mark.asyncio
async def test_full_health_proof_requires_all_mounts_same_process(
    workspace_runtime_controller_case,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    assert await verify_managed_backend_group(graph.owner.contract, mounts=registry.resolve_backend_mounts(graph.owner.contract)) == 7000


@pytest.mark.asyncio
async def test_force_restart_invalidates_all_leases_after_durable_interrupt_and_waits_for_release(
    workspace_runtime_controller_case, process_boundary,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    old_process = graph.owner.process
    leases = {member_id: registry.acquire_route_reference(member_id, streaming=True) for member_id in graph.owner.contract.workspace_ids}

    async def finish_old_streams() -> None:
        await asyncio.gather(*(lease.invalidated.wait() for lease in leases.values()))
        assert {member_id for member_id, path in runtime_actions if path.endswith("/force")} == set(leases)
        assert old_process is graph.owner.process and not old_process.closed
        for member_id in leases:
            with pytest.raises(LookupError, match="切换路由"):
                registry.resolve_service_url(member_id, "workspace_api")
            registry.release_route_reference(member_id, streaming=True)

    release = asyncio.create_task(finish_old_streams())
    try:
        result = await controller.force_restart_managed_backend(
            "member-a", request_id="force-streams", group_request=graph.group_request,
        )
        await release
    finally:
        if not release.done():
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)
        for member_id in leases:
            if registry.route_reference_counts(member_id)[1]:
                registry.release_route_reference(member_id, streaming=True)
    assert result.status == "restarted"
    assert old_process.closed and len(process_boundary.starts) == 1
    assert {blocker.workspace_id for blocker in result.blockers} == set(leases)
    assert all(registry.route_reference_counts(member_id) == (0, 0) for member_id in leases)


@pytest.mark.asyncio
async def test_force_restart_unreleased_reference_fails_closed_then_full_start_recovers(
    workspace_runtime_controller_case, process_boundary,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    controller._connection_drain_timeout_seconds = 0.005
    old_process = graph.owner.process
    lease = registry.acquire_route_reference("member-b", streaming=True)
    try:
        with pytest.raises(RuntimeError, match="member-b.*旧路由"):
            await controller.force_restart_managed_backend(
                "member-a", request_id="force-timeout", group_request=graph.group_request,
            )
        assert lease.invalidated.is_set()
        assert graph.owner.process is old_process and not old_process.closed
        assert not process_boundary.starts
        assert all(registry.resolve(member_id).connection_error for member_id in graph.owner.contract.workspace_ids)
        for member_id in graph.owner.contract.workspace_ids:
            with pytest.raises(RuntimeError, match="旧路由引用未释放"):
                registry.resolve_service_url(member_id, "workspace_api")
    finally:
        registry.release_route_reference("member-b", streaming=True)
    result = await controller.start_managed_backend(
        "member-a", request_id="recover-start", group_request=graph.group_request,
    )
    assert result.status == "started"
    assert old_process.closed and len(process_boundary.starts) == 1
    assert all(registry.resolve(member_id).connection_error is None for member_id in graph.owner.contract.workspace_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "restart-safe"])
async def test_commit_observer_error_keeps_published_contract_and_running_unique_owner(
    workspace_runtime_controller_case, process_boundary, action,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    if action == "start":
        registry.stop_backend_owner(graph.owner)
    old_contract = graph.owner.contract
    failure = RuntimeError("提交观察者失败")
    def observer() -> None:
        raise failure
    registry.add_commit_observer(observer)
    try:
        with pytest.raises(RuntimeError) as raised:
            method = controller.start_managed_backend if action == "start" else controller.safe_restart_managed_backend
            await method("member-a", request_id="observer", group_request=graph.group_request)
        assert raised.value is failure
        assert graph.owner.contract is not old_contract
        assert graph.owner.error is None
        assert graph.owner.process is process_boundary.handles[-1]
        assert not graph.owner.process.closed
        assert not graph.owner.unpublished_runtimes
        assert all(registry.managed_runtime(member_id).backend_owner is graph.owner for member_id in graph.owner.contract.workspace_ids)
        persisted = GatewayWorkspaceRegistry(storage_path=registry._storage_path)
        assert persisted.backend_owner("member-a").contract == graph.owner.contract
        assert any("已提交" in note for note in failure.__notes__)
    finally:
        registry.remove_commit_observer(observer)


@pytest.mark.asyncio
async def test_auxiliary_stop_failure_keeps_failed_handle_and_whole_group_intent_then_start_repairs(
    workspace_runtime_controller_case, process_boundary,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime = graph.runtimes["member-b"]
    failed = runtime.processes["browser_manager"]
    failed.failure = RuntimeError("browser 清理失败")
    with pytest.raises(RuntimeError, match="browser 清理失败"):
        registry.stop_backend_owner(graph.owner)
    assert graph.owner.process is None
    assert not registry.has_runtime("member-a")
    assert registry.managed_runtime("member-b") is runtime
    assert runtime.processes == {"browser_manager": failed}
    assert all(not registry.resolve(member_id).desired_running for member_id in graph.owner.contract.workspace_ids)
    assert all(registry.resolve(member_id).connection_error for member_id in graph.owner.contract.workspace_ids)
    failed.failure = None
    result = await controller.start_managed_backend("member-a", request_id="repair-aux", group_request=graph.group_request)
    assert result.status == "started" and len(process_boundary.starts) == 1
    assert all(registry.resolve(member_id).desired_running for member_id in graph.owner.contract.workspace_ids)
    assert all(registry.resolve(member_id).connection_error is None for member_id in graph.owner.contract.workspace_ids)
    assert set(runtime.processes) == {"terminal_manager", "browser_manager"}


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True])
async def test_empty_desired_releases_owner_once_without_empty_backend(workspace_runtime_controller_case, process_boundary, forced) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    blockers_by_workspace = workspace_runtime_controller_case.blockers_by_workspace
    old = graph.owner.process
    auxiliaries = [handle for runtime in graph.runtimes.values() for handle in runtime.processes.values()]
    if forced:
        blockers_by_workspace["member-b"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    request = graph.group_request.model_copy(update={"desired_workspace_ids": []})
    method = controller.force_restart_managed_backend if forced else controller.safe_restart_managed_backend
    result = await method("member-a", request_id="empty", group_request=request)
    assert result.status == "stopped" and result.forced is forced
    assert set(result.affected_workspace_ids) == {"member-a", "member-b"}
    assert old.events.count("close:old") == 1 and old.closed
    assert all(handle.closed for handle in auxiliaries)
    assert not registry.backend_owners() and not process_boundary.starts
    assert not graph.owner.unpublished_runtimes
    for identity in ("member-a", "member-b"):
        target = registry.resolve(identity)
        assert not target.desired_running and not target.local_service_urls and not target.backend_url
        assert not registry.has_runtime(identity) and registry.backend_owner(identity) is None
    persisted = GatewayWorkspaceRegistry(storage_path=registry._storage_path)
    assert not persisted.backend_owners()
    if forced:
        assert {identity for identity, path in runtime_actions if path.endswith("/force")} == {"member-a", "member-b"}


@pytest.mark.asyncio
async def test_initiator_can_exit_while_sibling_keeps_unique_owner(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    old = graph.owner.process
    exiting = list(graph.runtimes["member-a"].processes.values())
    kept = dict(graph.runtimes["member-b"].processes)
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]})
    result = await controller.safe_restart_managed_backend("member-a", request_id="exit-initiator", group_request=request)
    assert result.status == "restarted" and old.closed and len(process_boundary.starts) == 1
    assert graph.owner.contract.workspace_ids == ("member-b",)
    assert registry.backend_owner("member-a") is None
    assert registry.backend_owner("member-b") is graph.owner
    assert all(handle.closed for handle in exiting)
    assert graph.runtimes["member-b"].processes == kept and all(not handle.closed for handle in kept.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["job", "tool", "background_task"])
async def test_safe_exit_blockers_preserve_complete_contract_intents_and_auxiliary_services(workspace_runtime_controller_case, process_boundary, kind) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    lifecycle_states = workspace_runtime_controller_case.lifecycle_states
    blockers_by_workspace = workspace_runtime_controller_case.blockers_by_workspace
    contract = graph.owner.contract
    process = graph.owner.process
    runtimes = dict(graph.runtimes)
    auxiliaries = [handle for runtime in runtimes.values() for handle in runtime.processes.values()]
    blockers_by_workspace["member-b"] = [{"kind": kind, "resource_id": "active", "session_id": "session-b", "status": "running"}]
    result = await controller.safe_restart_managed_backend(
        "member-a", request_id="blocked-exit", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
    )
    assert result.status == "blocked" and result.blockers[0].workspace_id == "member-b"
    assert graph.owner.contract is contract and graph.owner.process is process
    assert not process.closed and not process_boundary.starts
    assert all(not handle.closed for handle in auxiliaries)
    assert all(registry.resolve(identity).desired_running and registry.managed_runtime(identity) is runtime for identity, runtime in runtimes.items())
    assert set(lifecycle_states.values()) == {"ready"}
    assert {identity for identity, path in runtime_actions if path.endswith("/cancel")} == {"member-a", "member-b"}


@pytest.mark.asyncio
async def test_default_exit_and_duplicate_mount_identity_reject_before_drain(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    registry.resolve("member-b").system_default = True
    with pytest.raises(PermissionError, match="默认工作区"):
        await controller.safe_restart_managed_backend(
            "member-a", request_id="default-exit", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
        )
    registry.resolve("member-b").system_default = False
    first = graph.owner.contract.members[0]
    duplicate = WorkspaceTarget(
        workspace_id="duplicate", name="Duplicate", root_path=str(registry.local_workspace_root(first.workspace_id, first.backend_workspace_id)),
        backend_url="", connection_kind="local", backend_workspace_id=first.backend_workspace_id, managed=True,
    )
    registry.upsert(duplicate, activate=False)
    with pytest.raises(ValueError, match="重复身份"):
        await controller.safe_restart_managed_backend(
            "member-a", request_id="duplicate", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["member-a", "duplicate"]}),
        )
    assert not runtime_actions and not process_boundary.starts and not graph.owner.process.closed


@pytest.mark.asyncio
async def test_controlled_delete_requires_group_confirmation_and_preserves_blocked_member(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    blockers_by_workspace = workspace_runtime_controller_case.blockers_by_workspace
    blocked = await controller.remove_managed_workspace("member-a", request_id="missing-contract")
    assert blocked.status == "blocked" and blocked.blockers[0].kind == "backend_group"
    assert not runtime_actions and not graph.owner.process.closed
    blockers_by_workspace["member-b"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]})
    blocked = await controller.remove_managed_workspace("member-a", request_id="safe-delete", group_request=request)
    assert blocked.status == "blocked" and registry.resolve("member-a").desired_running
    blockers_by_workspace["member-b"] = []
    result = await controller.remove_managed_workspace("member-a", request_id="delete", group_request=request)
    assert result.status == "restarted" and graph.owner.contract.workspace_ids == ("member-b",)
    with pytest.raises(LookupError):
        registry.resolve("member-a")
    assert len(process_boundary.starts) == 1


@pytest.mark.asyncio
async def test_force_exit_failed_durable_ack_never_closes_owner(workspace_runtime_controller_case, process_boundary, monkeypatch) -> None:
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    old = graph.owner.process
    action = controller._runtime_action
    async def fail_second(target, path, **kwargs):
        if target.workspace_id == "member-b" and path.endswith("/force"):
            runtime_actions.append((target.workspace_id, path))
            raise RuntimeError("member-b durable interrupted 提交失败")
        return await action(target, path, **kwargs)
    monkeypatch.setattr(controller, "_runtime_action", fail_second)
    with pytest.raises(RuntimeError, match="durable interrupted"):
        await controller.force_restart_managed_backend(
            "member-a", request_id="failed-ack", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
        )
    assert graph.owner.process is old and not old.closed and not process_boundary.starts
    assert {identity for identity, path in runtime_actions if path.endswith("/force")} == {"member-a", "member-b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["auxiliary", "publication"])
async def test_exit_failure_recovers_complete_old_group(workspace_runtime_controller_case, process_boundary, monkeypatch, failure) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    old_contract = graph.owner.contract
    if failure == "auxiliary":
        handle = graph.runtimes["member-b"].processes["browser_manager"]
        original = handle.close
        attempts = 0
        def close_once(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("auxiliary exit 失败")
            original(**kwargs)
        monkeypatch.setattr(handle, "close", close_once)
    else:
        original = registry._save
        def fail_exit(**kwargs):
            if kwargs.get("backend_contracts") == ():
                raise RuntimeError("exit publication CAS 失败")
            return original(**kwargs)
        monkeypatch.setattr(registry, "_save", fail_exit)
    with pytest.raises(RuntimeError, match="exit"):
        await controller.safe_restart_managed_backend(
            "member-a", request_id="recover-exit", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
        )
    assert graph.owner.contract == old_contract and graph.owner.error is None
    assert len(process_boundary.starts) == 1 and graph.owner.process is process_boundary.handles[-1]
    assert all(registry.backend_owner(identity) is graph.owner and registry.resolve(identity).desired_running for identity in old_contract.workspace_ids)
    assert all(set(registry.managed_runtime(identity).processes) == {"terminal_manager", "browser_manager"} for identity in old_contract.workspace_ids)
    assert GatewayWorkspaceRegistry(storage_path=registry._storage_path).backend_owner("member-a").contract == old_contract


@pytest.mark.asyncio
async def test_exit_after_stop_releases_contract_without_backend_start(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    await controller.stop_managed_backend("member-a", request_id="stop", group_request=graph.group_request)
    result = await controller.safe_restart_managed_backend(
        "member-a", request_id="exit-stopped", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
    )
    assert result.status == "stopped" and not registry.backend_owners() and not process_boundary.starts


@pytest.mark.asyncio
async def test_exit_commit_observer_error_cannot_restore_released_owner(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    failure = RuntimeError("exit observer 失败")
    def observer():
        raise failure
    registry.add_commit_observer(observer)
    try:
        with pytest.raises(RuntimeError) as raised:
            await controller.safe_restart_managed_backend(
                "member-a", request_id="exit-observer", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
            )
        assert raised.value is failure and not registry.backend_owners() and not process_boundary.starts
        assert graph.owner.process is None and not graph.owner.unpublished_runtimes
        assert not GatewayWorkspaceRegistry(storage_path=registry._storage_path).backend_owners()
    finally:
        registry.remove_commit_observer(observer)


def test_registry_cannot_bypass_controlled_managed_exit(workspace_runtime_controller_case) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    with pytest.raises(ValueError, match="受控退出"):
        registry.remove("member-a")
    with pytest.raises(ValueError, match="完整组"):
        registry.upsert(replace(registry.resolve("member-a")), runtime=WorkspaceRuntime(service_urls={}))
    assert registry.backend_owner("member-a") is graph.owner and not graph.owner.process.closed


@pytest.fixture
def deletion_http_application(workspace_runtime_controller_case, tmp_path, monkeypatch):
    registry = workspace_runtime_controller_case.registry
    controller = workspace_runtime_controller_case.controller
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, gateway_http_exception_handler)
    app.state.registry = registry
    app.state.workspace_runtime_controller = controller
    app.state.port_forward_manager = SshPortForwardManager(
        registry=registry, storage_path=tmp_path / "forwards.json", log_dir=tmp_path / "logs",
    )
    app.dependency_overrides[verify_gateway_token] = lambda: "local-test-token"
    app.dependency_overrides[verify_federation_token] = lambda: GatewayAuthContext(kind="federation", peer_gateway_id="peer")
    app.state.federation_policy_store = FederationPolicyStore(initial={"rules": [{
        "capability": "managed_workspace_admin", "effect": "allow", "principal_ref": principal_ref_for_gateway("peer"),
    }]})
    app.dependency_overrides[_shared.get_workspace_runtime_controller] = lambda: controller
    app.dependency_overrides[get_port_forward_manager] = lambda: app.state.port_forward_manager
    monkeypatch.setattr(_shared, "_gateway_root", lambda: tmp_path / "gateway-home")
    async def listed(*, check_health=True):
        return await GatewayWorkspaceRegistry.list_dtos(registry, check_health=False)
    monkeypatch.setattr(registry, "list_dtos", listed)
    for router in (workspaces_lifecycle.router, workspaces_managed.router, federation.router):
        app.include_router(router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("prefix", [
    "/api/gateway/workspaces",
    "/api/gateway/managed-workspaces",
    "/api/gateway/federation/managed-workspaces",
])
async def test_all_delete_http_callers_preserve_typed_blockers_and_consume_force_result(
    workspace_runtime_controller_case, deletion_http_application, process_boundary, prefix,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    blockers_by_workspace = workspace_runtime_controller_case.blockers_by_workspace
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]}).model_dump(mode="json")
    blockers_by_workspace["member-b"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    before = graph.owner.contract
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", f"{prefix}/member-a", json=request, headers={"X-Request-ID": "delete-blocked"})
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["status"] == "blocked" and detail["backend_generation"] == before.generation
        assert set(detail["affected_workspace_ids"]) == {"member-a", "member-b"}
        assert detail["blockers"][0]["workspace_id"] == "member-b"
        assert detail["blockers"][0]["resource_id"] == "job-b"
        assert {item["workspace_id"] for item in detail["workspaces"]["items"]} == {"member-a", "member-b"}
        assert response.headers["X-Request-ID"] == "delete-blocked"
        assert registry.backend_owner("member-a") is graph.owner and graph.owner.contract is before
        assert registry.resolve("member-a").desired_running and not process_boundary.starts
        response = await client.request("DELETE", f"{prefix}/member-a?force=true", json=request, headers={"X-Request-ID": "delete-forced"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert response.headers["X-Request-ID"] == result["request_id"] == "delete-forced"
        assert [item["workspace_id"] for item in result["data"]["items"]] == ["member-b"]
    assert graph.owner.contract.workspace_ids == ("member-b",) and len(process_boundary.starts) == 1
    assert {identity for identity, path in runtime_actions if path.endswith("/force")} == {"member-a", "member-b"}
    reopened = GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=registry._state_store)
    assert reopened.active_workspace_id == "member-b" and reopened.backend_owner("member-b").contract == graph.owner.contract
    assert {item["workspace_id"] for item in registry._state_store.load_workspace_registry()["targets"]} == {"member-b"}
    with pytest.raises(LookupError):
        reopened.resolve("member-a")
    with pytest.raises(LookupError):
        registry.resolve("member-a")


@pytest.mark.asyncio
async def test_delete_rejects_unrelated_desired_set_before_any_drain(workspace_runtime_controller_case, deletion_http_application) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    request = graph.group_request.model_copy(update={"desired_workspace_ids": []}).model_dump(mode="json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", "/api/gateway/workspaces/member-a", json=request)
    assert response.status_code == 409 and "其余完整成员" in response.json()["detail"]
    assert not runtime_actions and not graph.owner.process.closed and registry.resolve("member-a").desired_running


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [
    "/api/gateway/workspaces",
    "/api/gateway/managed-workspaces",
    "/api/gateway/federation/managed-workspaces",
])
async def test_delete_force_failed_backend_ack_reports_detail_and_keeps_unique_process(
    workspace_runtime_controller_case, deletion_http_application, process_boundary, monkeypatch, prefix,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    old = graph.owner.process
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]}).model_dump(mode="json")
    action = controller._runtime_action
    async def failed_ack(target, path, **kwargs):
        if target.workspace_id == "member-b" and path.endswith("/force"):
            raise httpx.ConnectError("member-b durable ack 连接断开")
        return await action(target, path, **kwargs)
    monkeypatch.setattr(controller, "_runtime_action", failed_ack)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", f"{prefix}/member-a?force=true", json=request)
    assert response.status_code == 502, response.text
    assert "member-b durable ack" in response.json()["detail"]
    assert graph.owner.process is old and not old.closed and not process_boundary.starts
    assert registry.backend_owner("member-a") is graph.owner and registry.resolve("member-a").desired_running


@pytest.fixture
async def stopped_workspace_runtime_controller_case(workspace_runtime_controller_case, process_boundary):
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    await controller.stop_managed_backend("member-a", request_id="stopped-fixture", group_request=graph.group_request)
    assert graph.owner.contract is previous and graph.owner.process is None
    assert not process_boundary.starts
    runtime_actions.clear()
    return workspace_runtime_controller_case


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize("prefix", [
    "/api/gateway/workspaces",
    "/api/gateway/managed-workspaces",
    "/api/gateway/federation/managed-workspaces",
])
async def test_stopped_delete_http_preserves_offline_contract_then_starts_only_remaining_mount(
    stopped_workspace_runtime_controller_case, deletion_http_application, process_boundary, prefix, forced,
) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    stale = graph.group_request
    request = stale.model_copy(update={"desired_workspace_ids": ["member-b"]}).model_dump(mode="json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", f"{prefix}/member-a?force={str(forced).lower()}", json=request)
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert [item["workspace_id"] for item in items] == ["member-b"]
    assert items[0]["status"] == "offline"
    # 受管/联邦列表保留各自既有精简 DTO；完整生命周期投影取同一 registry 权威状态。
    remaining = next(item.model_dump(mode="json") for item in await GatewayWorkspaceRegistry.list_dtos(registry, check_health=False) if item.workspace_id == "member-b")
    assert remaining["status"] == "offline" and remaining["affected_workspace_ids"] == ["member-b"]
    assert all(service["local_url"] is None for service in remaining["services"].values())
    if prefix == "/api/gateway/workspaces":
        assert items[0]["backend_generation"] == remaining["backend_generation"]
        assert items[0]["affected_workspace_ids"] == remaining["affected_workspace_ids"]
        assert items[0]["services"] == remaining["services"]
    contract = graph.owner.contract
    assert contract.owner_id == previous.owner_id and contract.generation != previous.generation
    assert contract.members == (previous.members[1],) and remaining["backend_generation"] == contract.generation
    assert graph.owner.process is None and not process_boundary.starts and not runtime_actions
    assert not registry.has_runtime("member-b") and not registry.resolve("member-b").desired_running
    assert registry.resolve("member-b").runtime_lease_id is None
    restored = GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=registry._state_store)
    assert restored.backend_owner("member-b").contract == contract
    assert not restored.resolve("member-b").desired_running and not restored.has_runtime("member-b")
    with pytest.raises(LookupError):
        registry.resolve("member-a")
    with pytest.raises(ValueError, match="陈旧"):
        await controller.start_managed_backend("member-b", request_id="stale", group_request=stale)
    started = await controller.start_managed_backend("member-b", request_id="remaining-start", group_request=graph.group_request)
    assert started.status == "started" and len(process_boundary.starts) == 1
    mounts = process_boundary.starts[0]["mounts"]
    assert [(mount.workspace_id, mount.workspace_root) for mount in mounts] == [(UUID_B, registry.local_workspace_root("member-b", UUID_B))]
    assert graph.owner.contract.workspace_ids == ("member-b",)
    assert await verify_managed_backend_group(graph.owner.contract, mounts=registry.resolve_backend_mounts(graph.owner.contract)) == 7001


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_member_exit_keeps_registration_without_restarting_backend(stopped_workspace_runtime_controller_case, process_boundary) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    result = await controller.safe_restart_managed_backend(
        "member-a", request_id="offline-exit", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]}),
    )
    assert result.status == "stopped" and result.affected_workspace_ids == ["member-a", "member-b"]
    assert graph.owner.contract.generation != previous.generation and graph.owner.contract.members == (previous.members[1],)
    assert registry.backend_owner("member-a") is None and registry.backend_owner("member-b") is graph.owner
    assert registry.resolve("member-a").backend_url == "" and not registry.resolve("member-a").local_service_urls
    assert all(not registry.resolve(identity).desired_running and registry.resolve(identity).runtime_lease_id is None for identity in previous.workspace_ids)
    assert all(item.status == "offline" for item in await GatewayWorkspaceRegistry.list_dtos(registry, check_health=False))
    assert not process_boundary.starts and not runtime_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_same_member_set_preserves_generation(stopped_workspace_runtime_controller_case, process_boundary) -> None:
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    result = await controller.safe_restart_managed_backend(
        "member-a", request_id="same-offline-set", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b", "member-a"]}),
    )
    assert result.status == "stopped" and graph.owner.contract == previous
    assert graph.owner.contract.generation == previous.generation and graph.owner.process is None
    assert not process_boundary.starts and not runtime_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("invalid", ["generation", "current", "desired", "default", "attached", "uuid", "root"])
async def test_stopped_delete_preflight_keeps_complete_old_contract(
    stopped_workspace_runtime_controller_case, process_boundary, monkeypatch, invalid,
) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    target = registry.resolve("member-a")
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]})
    exception, message = ValueError, "陈旧"
    if invalid == "generation":
        request = request.model_copy(update={"expected_backend_generation": "stale"})
    elif invalid == "current":
        request = request.model_copy(update={"expected_workspace_ids": ["member-b"]})
        message = "精确匹配"
    elif invalid == "desired":
        request = request.model_copy(update={"desired_workspace_ids": []})
        message = "其余完整成员"
    elif invalid == "default":
        monkeypatch.setattr(target, "system_default", True)
        exception, message = PermissionError, "默认工作区"
    elif invalid == "attached":
        monkeypatch.setattr(target, "owner", "attached")
        exception, message = PermissionError, "不能越过 target owner"
    elif invalid == "uuid":
        monkeypatch.setattr(target, "backend_workspace_id", UUID_B)
        exception, message = RuntimeError, "binding 身份不匹配"
    else:
        monkeypatch.setattr(target, "root_path", str(Path(target.root_path) / "changed"))
        exception, message = FileNotFoundError, "工作区目录不存在"
    with pytest.raises(exception, match=message):
        await controller.remove_managed_workspace("member-a", request_id="offline-invalid", group_request=request)
    assert graph.owner.contract is previous and registry.resolve("member-a") is target
    assert graph.owner.process is None and not runtime_actions and not process_boundary.starts
    assert not target.desired_running


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_delete_safe_blocks_route_references_and_force_waits_for_full_union(
    stopped_workspace_runtime_controller_case, process_boundary,
) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]})
    streams = {"member-a": False, "member-b": True}
    leases = {identity: registry.acquire_route_reference(identity, streaming=streaming) for identity, streaming in streams.items()}
    async def release_invalidated() -> None:
        await asyncio.gather(*(lease.invalidated.wait() for lease in leases.values()))
        assert graph.owner.contract is previous and not runtime_actions and not process_boundary.starts
        for identity, streaming in streams.items():
            registry.release_route_reference(identity, streaming=streaming)
    release = None
    try:
        blocked = await controller.remove_managed_workspace("member-a", request_id="offline-blocked", group_request=request)
        assert blocked.status == "blocked" and {(item.workspace_id, item.kind) for item in blocked.blockers} == {("member-a", "proxy_request"), ("member-b", "proxy_stream")}
        assert graph.owner.contract is previous and not any(lease.invalidated.is_set() for lease in leases.values())
        release = asyncio.create_task(release_invalidated())
        result = await controller.remove_managed_workspace("member-a", request_id="offline-force", force=True, group_request=request)
        await release
        assert result.status == "stopped" and result.forced
        assert {item.workspace_id for item in result.blockers} == set(streams)
        assert graph.owner.contract.workspace_ids == ("member-b",) and graph.owner.process is None
        assert not process_boundary.starts and not runtime_actions
    finally:
        if release is not None and not release.done():
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)
        for identity, streaming in streams.items():
            if registry._route_reference_counts.get(identity, (0, 0))[int(streaming)]:
                registry.release_route_reference(identity, streaming=streaming)


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_force_unreleased_route_keeps_old_generation_and_registration(stopped_workspace_runtime_controller_case, process_boundary) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    controller._connection_drain_timeout_seconds = 0.005
    previous = graph.owner.contract
    lease = registry.acquire_route_reference("member-b", streaming=True)
    try:
        with pytest.raises(RuntimeError, match="旧路由引用未释放"):
            await controller.remove_managed_workspace(
                "member-a", request_id="offline-route-timeout", force=True,
                group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]}),
            )
        assert lease.invalidated.is_set() and graph.owner.contract is previous
        assert registry.resolve("member-a").connection_error and not registry.resolve("member-a").desired_running
        assert not runtime_actions and not process_boundary.starts and graph.owner.process is None
        assert GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=registry._state_store).backend_owner("member-a").contract == previous
    finally:
        registry.release_route_reference("member-b", streaming=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("failure_kind", ["remaining_auxiliary", "unpublished", "cas"])
async def test_stopped_delete_cleanup_or_cas_failure_preserves_old_contract_then_retries(
    workspace_runtime_controller_case, process_boundary, monkeypatch, failure_kind,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    failure = RuntimeError(f"offline-{failure_kind}-failure")
    failed_handle = None
    if failure_kind == "remaining_auxiliary":
        failed_handle = graph.runtimes["member-b"].processes["browser_manager"]
        failed_handle.failure = failure
        with pytest.raises(RuntimeError, match="成员辅助服务关闭失败"):
            await controller.stop_managed_backend("member-a", request_id="partial-stop", group_request=graph.group_request)
    else:
        await controller.stop_managed_backend("member-a", request_id="full-stop", group_request=graph.group_request)
    runtime_actions.clear()
    if failure_kind == "unpublished":
        failed_handle = ProcessProbe("unpublished", failure=failure)
        graph.owner.unpublished_runtimes["member-a"] = WorkspaceRuntime(service_urls={}, processes={"browser_manager": failed_handle})
    original_save = registry._save
    if failure_kind == "cas":
        def fail_candidate(**kwargs):
            contracts = kwargs.get("backend_contracts")
            if contracts is not None and any(contract.owner_id == previous.owner_id and contract.workspace_ids == ("member-b",) for contract in contracts):
                raise failure
            return original_save(**kwargs)
        monkeypatch.setattr(registry, "_save", fail_candidate)
    targets = {identity: registry.resolve(identity) for identity in previous.workspace_ids}
    leases = {identity: target.runtime_lease_id for identity, target in targets.items()}
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]})
    try:
        with pytest.raises(RuntimeError, match=f"offline-{failure_kind}-failure"):
            await controller.remove_managed_workspace("member-a", request_id="offline-failed", group_request=request)
        assert graph.owner.contract is previous and graph.owner.process is None
        assert all(registry.resolve(identity) is target and not target.desired_running and target.runtime_lease_id == leases[identity] for identity, target in targets.items())
        assert GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=registry._state_store).backend_owner("member-a").contract == previous
        if failure_kind == "remaining_auxiliary":
            assert registry.managed_runtime("member-b") is graph.runtimes["member-b"] and not failed_handle.closed
        if failure_kind == "unpublished":
            assert graph.owner.unpublished_runtimes["member-a"].processes["browser_manager"] is failed_handle
        assert not process_boundary.starts and not runtime_actions
    finally:
        if failed_handle is not None:
            failed_handle.failure = None
        monkeypatch.setattr(registry, "_save", original_save)
    result = await controller.remove_managed_workspace("member-a", request_id="offline-retry", group_request=request)
    assert result.status == "stopped" and graph.owner.contract.workspace_ids == ("member-b",)
    assert graph.owner.contract.generation != previous.generation and graph.owner.error is None
    assert not graph.owner.unpublished_runtimes and not registry.has_runtime("member-b") and not process_boundary.starts


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_delete_commit_observer_error_keeps_new_offline_authority(stopped_workspace_runtime_controller_case, process_boundary) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    failure = RuntimeError("offline commit observer failed")
    def observer():
        raise failure
    registry.add_commit_observer(observer)
    try:
        with pytest.raises(RuntimeError) as raised:
            await controller.remove_managed_workspace(
                "member-a", request_id="offline-observer",
                group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]}),
            )
        assert raised.value is failure and graph.owner.contract.generation != previous.generation
        assert graph.owner.contract.members == (previous.members[1],) and graph.owner.process is None and graph.owner.error is None
        assert not registry.resolve("member-b").desired_running and registry.resolve("member-b").runtime_lease_id is None
        with pytest.raises(LookupError):
            registry.resolve("member-a")
        restored = GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=registry._state_store)
        assert restored.backend_owner("member-b").contract == graph.owner.contract
        with pytest.raises(LookupError):
            restored.resolve("member-a")
        assert not runtime_actions and not process_boundary.starts
    finally:
        registry.remove_commit_observer(observer)


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("boundary", ["no_locks", "owner_only", "topology_only", "process", "unpublished", "online_incomplete"])
async def test_stopped_publication_requires_closed_owner_and_both_locks(stopped_workspace_runtime_controller_case, boundary) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    previous = graph.owner.contract
    owner_locked = boundary in {"owner_only", "process", "unpublished"}
    topology_locked = boundary in {"topology_only", "process", "unpublished"}
    if owner_locked:
        await graph.owner.lock.acquire()
    if topology_locked:
        await registry.backend_topology_lock.acquire()
    if boundary == "process":
        graph.owner.set_process(ProcessProbe("residual"))
    if boundary == "unpublished":
        graph.owner.unpublished_runtimes["member-a"] = WorkspaceRuntime(service_urls={})
    try:
        exception = ValueError if boundary == "online_incomplete" else RuntimeError
        message = "完整成员 runtime" if boundary == "online_incomplete" else "受控关闭"
        with pytest.raises(exception, match=message):
            registry.publish_backend_runtime(graph.owner, previous, {} if boundary == "online_incomplete" else None)
        assert graph.owner.contract is previous and not registry.resolve("member-a").desired_running
    finally:
        if owner_locked:
            graph.owner.lock.release()
        if topology_locked:
            registry.backend_topology_lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("action", ["stop", "restart-safe", "restart-force"])
async def test_stopped_ordinary_action_without_desired_set_keeps_existing_rejection(
    stopped_workspace_runtime_controller_case, process_boundary, action, monkeypatch,
) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    async def unexpected_pending(*_, **__):
        raise AssertionError("普通停机动作不能进入配置 pending 启动入口")
    monkeypatch.setattr(controller, "_pending_startup_contract", unexpected_pending)
    invoke = {
        "stop": controller.stop_managed_backend,
        "restart-safe": controller.safe_restart_managed_backend,
        "restart-force": controller.force_restart_managed_backend,
    }[action]
    with pytest.raises(ValueError, match="尚未启动"):
        await invoke("member-a", request_id="ordinary-stopped", group_request=graph.group_request)
    assert graph.owner.contract is previous and graph.owner.error is None and graph.owner.process is None
    assert registry.backend_owner("member-a") is graph.owner and not process_boundary.starts and not runtime_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_delete_sqlite_atomically_detaches_other_owner_child_without_touching_its_backend(
    stopped_workspace_runtime_controller_case, managed_backend_factory, process_boundary, tmp_path,
) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    other_root = tmp_path / "other-owner"
    other_root.mkdir()
    other = WorkspaceTarget(
        workspace_id="other-owner-member", name="Other owner", root_path=str(other_root),
        backend_url="http://127.0.0.1:41001", connection_kind="local", managed=True,
        backend_workspace_id="00000000-0000-4000-8000-000000000003", owner="config", parent_workspace_id="member-a",
    )
    registry.upsert(other, activate=False)
    other_graph = managed_backend_factory((other,), backend_url=other.backend_url, process=ProcessProbe("other-backend"))
    other_graph.publish(registry)
    previous_other = registry.resolve(other.workspace_id)
    result = await controller.remove_managed_workspace(
        "member-a", request_id="sqlite-exact-delete",
        group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]}),
    )
    assert result.status == "stopped" and graph.owner.contract.workspace_ids == ("member-b",)
    assert registry.resolve(other.workspace_id) == replace(previous_other, parent_workspace_id=None)
    assert registry.backend_owner(other.workspace_id) is other_graph.owner
    assert registry.managed_runtime(other.workspace_id) is other_graph.runtimes[other.workspace_id]
    assert other_graph.owner.process.is_alive() and not other_graph.owner.process.closed
    saved = registry._state_store.load_workspace_registry()
    assert {target["workspace_id"] for target in saved["targets"]} == {"member-b", other.workspace_id}
    assert next(target for target in saved["targets"] if target["workspace_id"] == other.workspace_id)["parent_workspace_id"] is None
    assert len(saved["managed_backends"]) == 2
    reopened = GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=registry._state_store)
    assert reopened.resolve(other.workspace_id) == registry.resolve(other.workspace_id)
    assert reopened.backend_owner(other.workspace_id).contract == other_graph.owner.contract
    assert reopened.backend_owner("member-b").contract == graph.owner.contract
    with pytest.raises(LookupError):
        reopened.resolve("member-a")
    assert not process_boundary.starts and not runtime_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_delete_sqlite_real_cas_failure_preserves_complete_old_authority(
    stopped_workspace_runtime_controller_case, process_boundary,
) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    controller = stopped_workspace_runtime_controller_case.controller
    runtime_actions = stopped_workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    held_targets = {identity: registry.resolve(identity) for identity in previous.workspace_ids}
    writer = GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=registry._state_store)
    writer.rename("member-b", "Concurrent rename")
    committed = registry._state_store.load_workspace_registry()
    with pytest.raises(RuntimeError, match="CAS") as raised:
        await controller.remove_managed_workspace(
            "member-a", request_id="sqlite-real-cas",
            group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["member-b"]}),
        )
    assert graph.owner.contract is previous and graph.owner.process is None
    assert registry._state_store.load_workspace_registry() == committed
    assert any("记录停机成员退出失败状态" in note and "CAS" in note for note in raised.value.__notes__)
    assert all(registry.resolve(identity) is target and not target.desired_running for identity, target in held_targets.items())
    assert not process_boundary.starts and not runtime_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
async def test_stopped_publication_online_null_member_runtime_fails_before_commit(stopped_workspace_runtime_controller_case) -> None:
    registry = stopped_workspace_runtime_controller_case.registry
    graph = stopped_workspace_runtime_controller_case.graph
    previous = graph.owner.contract
    held_targets = {identity: registry.resolve(identity) for identity in previous.workspace_ids}
    committed = registry._state_store.load_workspace_registry()
    with pytest.raises(AttributeError, match="service_urls"):
        registry.publish_backend_runtime(graph.owner, previous, {identity: None for identity in previous.workspace_ids})
    assert graph.owner.contract is previous and registry._state_store.load_workspace_registry() == committed
    assert all(registry.resolve(identity) is target for identity, target in held_targets.items())


@pytest.fixture
def paired_deletion_http_application(deletion_http_application, tmp_path, monkeypatch):
    from app.gateway.credentials import FederationCredentialStore
    app = deletion_http_application
    app.dependency_overrides.pop(verify_federation_token)
    gateway_root = tmp_path / "paired-gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    credential = FederationCredentialStore(storage_path=gateway_root / "credentials/federation.json").issue(
        connection_id="paired-peer", peer_gateway_id="peer",
    )
    return app, {"X-BoxTeam-Federation-Token": credential.token, "X-Request-ID": "root-admin-denied"}


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", [
    [],
    [{"capability": "read", "effect": "allow", "principal_ref": principal_ref_for_gateway("peer")}],
    [{"capability": "managed_workspace_admin", "effect": "allow"}],
    [{"capability": "managed_workspace_admin", "effect": "allow", "principal_ref": principal_ref_for_gateway("other")}],
])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_paired_peer_without_explicit_admin_has_zero_side_effects(
    workspace_runtime_controller_case, process_boundary, paired_deletion_http_application, tmp_path, rules, method,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    app, headers = paired_deletion_http_application
    app.state.federation_policy_store.publish({"default_effect": "allow", "rules": rules})
    before = registry._storage_path.read_bytes()
    revision = registry.registry_revision
    contract = graph.owner.contract
    process = graph.owner.process
    runtimes = dict(graph.runtimes)
    root = tmp_path / "must-not-exist" / "new-workspace"
    path = "/api/gateway/federation/managed-workspaces"
    if method == "DELETE":
        path += "/member-a"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://target") as client:
        response = await client.request(method, path, headers=headers, json={"root_path": str(root), "create_directory": True} if method == "POST" else None)
    assert response.status_code == 403
    assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
    assert response.json()["request_id"] == headers["X-Request-ID"]
    assert registry._storage_path.read_bytes() == before and registry.registry_revision == revision
    assert graph.owner.contract is contract and graph.owner.process is process and not process.closed
    assert not runtime_actions and not process_boundary.starts and not process_boundary.nodes and not root.exists()
    assert all(registry.managed_runtime(identity) is runtime and all(not handle.closed for handle in runtime.processes.values()) for identity, runtime in runtimes.items())


@pytest.mark.asyncio
async def test_admin_revocation_is_checked_before_delete_side_effects(
    workspace_runtime_controller_case, process_boundary, paired_deletion_http_application,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    app, headers = paired_deletion_http_application
    policy = app.state.federation_policy_store
    policy.publish({"rules": [{"capability": "managed_workspace_admin", "effect": "allow", "principal_ref": principal_ref_for_gateway("peer")}]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://target") as client:
        blocked = await client.delete("/api/gateway/federation/managed-workspaces/member-a", headers=headers)
        assert blocked.status_code == 409
        before = registry._storage_path.read_bytes()
        policy.publish({"rules": []})
        denied = await client.delete("/api/gateway/federation/managed-workspaces/member-a", headers=headers)
    assert denied.status_code == 403 and registry._storage_path.read_bytes() == before
    assert not runtime_actions and not graph.owner.process.closed and not process_boundary.starts
