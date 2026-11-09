"""完整 Backend owner 的生命周期、proof 与一次接管边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import app.gateway.runtime.controller as runtime_controller_module
from app.core import failures
from app.core.backend_identity import load_or_create_backend_id, read_backend_id
from app.core.mounted_workspaces import (
    MountedWorkspace,
    mounted_workspace_manifest_digest,
)
from app.core.path_utils import get_boxteam_home
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import (
    GatewayAuthContext,
    verify_federation_token,
    verify_gateway_access,
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
    _adopt_workspace_backend,
    rebuild_managed_backend_runtime,
    start_managed_backend_runtime,
    verify_managed_backend_group,
    wait_for_workspace_health,
    workspace_config_proof_expectation,
)
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.port_forwarding import get_port_forward_manager
from tests.support.gateway_managed_backends import ManagedBackendGraph, ProcessProbe

REMOTE_GATEWAY_ID = "gateway_00000000000040008000000000000002"
GATEWAY_ID = "gateway_00000000000040008000000000000001"

UUID_A = "00000000-0000-4000-8000-000000000001"
UUID_B = "00000000-0000-4000-8000-000000000002"
UUID_C = "00000000-0000-4000-8000-000000000003"


@dataclass
class ProcessBoundary:
    backend_id: str
    identities: dict[Path, str] = field(default_factory=dict)
    proofs: dict[str, tuple[tuple[MountedWorkspace, ...], str, int]] = field(default_factory=dict)
    config_proofs: dict[str, dict[str, object]] = field(default_factory=dict)
    starts: list[dict[str, object]] = field(default_factory=list)
    handles: list[ProcessProbe] = field(default_factory=list)
    backend_outcomes: list[Exception | None] = field(default_factory=list)
    failed_urls: dict[str, Exception] = field(default_factory=dict)
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

    async def health(self, url: str, workspace_id: str, *_: object, **__: object) -> dict[str, object]:
        if "/api/v1/health" not in url:
            return {"workspace_id": workspace_id, "process_id": 8000}
        backend_url = url.removesuffix("/api/v1/health")
        if backend_url in self.failed_urls:
            raise self.failed_urls[backend_url]
        mounts, generation, pid = self.proofs[backend_url]
        assert workspace_id in {mount.workspace_id for mount in mounts}
        result = {
            "backend_id": self.backend_id,
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
    boundary = ProcessBoundary(backend_id=load_or_create_backend_id(get_boxteam_home()))
    monkeypatch.setattr(local_workspace, "resolve_backend_workspace_id", lambda _project, root, **_: boundary.identities[root.resolve()])
    monkeypatch.setattr("app.gateway.runtime.group_actions.resolve_backend_workspace_id", lambda _project, root, **_: boundary.identities[root.resolve()])
    monkeypatch.setattr(local_workspace, "allocate_local_port", boundary.allocate)
    monkeypatch.setattr(local_workspace, "is_local_port_available", lambda _port: True)
    monkeypatch.setattr(local_workspace, "start_local_backend_process", boundary.start_backend)
    monkeypatch.setattr(local_workspace, "wait_for_workspace_health", boundary.health)

    async def no_adoption(**_: object) -> None:
        return None

    monkeypatch.setattr(local_workspace, "_adopt_workspace_backend", no_adoption)
    return boundary


@pytest.fixture
def single_workspace_graph(tmp_path: Path, managed_backend_factory, process_boundary: ProcessBoundary) -> ManagedBackendGraph:
    target = WorkspaceTarget(gateway_id=GATEWAY_ID,
        workspace_id=UUID_A, name="A", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
         managed=True,
    )
    graph = managed_backend_factory((target,), backend_url=target.backend_url, process=ProcessProbe("old"))
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID,
        storage_path=tmp_path / "single-workspace-registry.json"
    )
    registry.upsert(target)
    graph.publish(registry)
    process_boundary.register(graph, registry)
    return graph


@pytest.fixture
def single_workspace_registry(tmp_path: Path, single_workspace_graph: ManagedBackendGraph):
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "runtime-registry.json")
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
        targets.append(WorkspaceTarget(gateway_id=GATEWAY_ID,
            workspace_id=identity, name=name, root_path=str(root),
            backend_url="http://127.0.0.1:41000", connection_kind="local",
             managed=True,
        ))
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "gateway.json", state_store=controller_registry_store)
    for target in targets:
        registry.upsert(target, activate=False)
    graph = managed_backend_factory(tuple(targets), backend_url=targets[0].backend_url, process=ProcessProbe("old"))
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
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "gateway.json")
    target = WorkspaceTarget(gateway_id=GATEWAY_ID,
        workspace_id=UUID_A, name="Managed", root_path=str(tmp_path),
        backend_url=single_workspace_graph.owner.contract.backend_url,
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
    caplog: pytest.LogCaptureFixture,
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
        remote_gateway_id=REMOTE_GATEWAY_ID,
        protocol_version=1,
        source_owner="manual",
    )
    workspace_id = UUID_B
    old_runtime = _TrackingRuntime("http://127.0.0.1:41001")
    new_runtime = _TrackingRuntime("http://127.0.0.1:42001")
    old_target = WorkspaceTarget(gateway_id=REMOTE_GATEWAY_ID,
        workspace_id=UUID_B,
        name="Reconnect HTTP workspace",
        root_path=None,
        backend_url=old_runtime.service_urls["workspace_api"],
        connection_kind="remote_gateway",

        owner="remote_projection",
        connection_id=connection.connection_id,
        remote_gateway_connection_ids=(connection.connection_id,),

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
                        headers={"X-Request-ID": "reconnect-postcommit-01", "X-BoxTeam-Target-Gateway-Id": REMOTE_GATEWAY_ID},
                    )
                assert cancelled.value.__context__ is primary_error
            else:
                response = await client.post(
                    f"/api/gateway/workspaces/{workspace_id}/reconnect",
                    headers={"X-Request-ID": "reconnect-postcommit-01", "X-BoxTeam-Target-Gateway-Id": REMOTE_GATEWAY_ID},
                )
                assert response.status_code == 502, response.text
                assert response.headers["X-Request-ID"] == "reconnect-postcommit-01"
                assert response.json() == {
                    "detail": "工作区重连失败: stage=workspace_reconnect, error=RuntimeError",
                    "request_id": "reconnect-postcommit-01",
                }
                record = next(record for record in caplog.records if record.exc_info and record.exc_info[1] is primary_error)
                assert record.exc_text is not None
                assert "observer failed after remote projection commit" in record.exc_text
                assert "port-forward reconcile failed after commit" in record.exc_text

        assert primary_error.args == (
            "observer failed after remote projection commit",
        )
        assert reconcile_calls == 1
        assert registry.resolve((REMOTE_GATEWAY_ID, workspace_id)).backend_url == "http://127.0.0.1:42001"
        assert registry.resolve((REMOTE_GATEWAY_ID, workspace_id)).connection_error is None
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
@pytest.mark.parametrize("native_location", ["/private/reconnect-refusal", r"C:\private\reconnect-refusal", r"\\server\private\reconnect-refusal"])
@pytest.mark.parametrize(("error_type", "status"), [(LookupError, 404), (ValueError, 409), (PermissionError, 409)])
async def test_reconnect_refusal_keeps_status_without_publishing_exception_body(
    workspace_runtime_controller_case,
    deletion_http_application,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    native_location: str,
    error_type: type[Exception],
    status: int,
) -> None:
    registry = workspace_runtime_controller_case.registry
    error = error_type(native_location)
    previous_commit = registry._last_committed_snapshot

    async def reject_reconnect(_workspace_id: str) -> None:
        raise error

    monkeypatch.setattr(workspace_runtime_controller_case.controller, "reconnect_ssh", reject_reconnect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.post("/api/gateway/workspaces/00000000-0000-4000-8000-000000000001/reconnect", headers={"X-Request-ID": "reconnect-refusal-01"})
    assert response.status_code == status
    assert response.json() == {
        "detail": f"工作区重连失败: stage=workspace_reconnect, error={error_type.__name__}" + (", errno=None" if error_type is PermissionError else ""),
        "request_id": "reconnect-refusal-01",
    }
    assert response.headers["X-Request-ID"] == "reconnect-refusal-01"
    assert registry._last_committed_snapshot is previous_commit
    assert registry.resolve("00000000-0000-4000-8000-000000000001").connection_error is None
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert error.args == (native_location,)


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("native_location", ["/private/reconnect-failure", r"C:\private\reconnect-failure", r"\\server\private\reconnect-failure"])
@pytest.mark.parametrize("record_fails", [False, True])
@pytest.mark.parametrize("sink_fails", [False, True])
async def test_reconnect_http_preserves_primary_error_when_connection_error_recording_fails(
    workspace_runtime_controller_case,
    deletion_http_application,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    native_location: str,
    record_fails: bool,
    sink_fails: bool,
    tmp_path: Path,
) -> None:
    registry = workspace_runtime_controller_case.registry
    controller = workspace_runtime_controller_case.controller
    target = WorkspaceTarget(gateway_id=GATEWAY_ID,
        workspace_id='00000000-0000-4000-8000-000000000003',
        name="External workspace",
        root_path="/external/reconnect-error",
        backend_url="http://127.0.0.1:43001",
        connection_kind="local",

        managed=False,
    )
    registry.upsert(
        target,
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": target.backend_url},
            workspace_id=target.workspace_id,
        ),
        activate=False,
    )
    primary_error = OSError(5, "reconnect failed before registry commit", native_location)
    primary_error.add_note(f"原始连接诊断: {native_location}")
    secondary_error = RuntimeError(f"connection_error persistence failed: {native_location}")
    sink_error = OSError(5, "diagnostic sink failed", native_location)
    calls = 0
    recorded_errors: list[str] = []
    mark_connection_error = registry.mark_connection_error

    async def fail_reconnect(_workspace_id: str) -> None:
        raise primary_error

    def fail_mark_connection_error(_workspace_id: str, _error: str) -> None:
        nonlocal calls
        calls += 1
        recorded_errors.append(_error)
        if record_fails:
            raise secondary_error
        mark_connection_error(_workspace_id, _error)

    monkeypatch.setattr(controller, "reconnect_ssh", fail_reconnect)
    monkeypatch.setattr(registry, "mark_connection_error", fail_mark_connection_error)
    if sink_fails:
        def reject_log(*_args: object, **_kwargs: object) -> None:
            raise sink_error
        monkeypatch.setattr(failures.logger, "error", reject_log)
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
    expected = "工作区重连失败: stage=workspace_reconnect, error=OSError, errno=5"
    if sink_fails:
        expected += ", diagnostic_sink_error=OSError"
    secondary_detail = "; 记录连接失败状态失败: stage=workspace_reconnect_record, error=RuntimeError"
    if sink_fails:
        secondary_detail += ", diagnostic_sink_error=OSError"
    assert response.json() == {
        "detail": expected + (secondary_detail if record_fails else ""),
        "request_id": "reconnect-mark-error-secondary-01",
    }
    assert primary_error.args == (5, "reconnect failed before registry commit")
    assert primary_error.filename == native_location
    assert recorded_errors == [expected]
    if sink_fails:
        assert any("诊断日志输出失败" in note and str(sink_error) in note for note in primary_error.__notes__)
    else:
        record = next(record for record in caplog.records if record.exc_info and record.exc_info[1] is primary_error)
        assert record.exc_info is not None and record.exc_text is not None
        assert record.exc_info[1] is primary_error
        assert f"原始连接诊断: {native_location}" in record.exc_text
    if record_fails:
        if sink_fails:
            assert any("诊断日志输出失败" in note and str(sink_error) in note for note in secondary_error.__notes__)
        else:
            assert any(record.exc_info and record.exc_info[1] is secondary_error for record in caplog.records)
        assert any(str(secondary_error) in note for note in primary_error.__notes__)
    assert calls == 1
    assert registry.registry_revision == previous_revision + (0 if record_fails else 1)
    assert (registry._last_committed_snapshot is previous_commit) is record_fails
    assert state.get_registry_revision() == registry.registry_revision
    expected_record = None if record_fails else expected
    assert registry.resolve(target.workspace_id).connection_error == expected_record
    # 从真实 SQLite 重新加载并经 DTO 序列化，不能只证明路由的响应格式。
    cold = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "cold-registry.json", state_store=state)
    assert cold.resolve(target.workspace_id).connection_error == expected_record
    items = await cold.list_dtos(check_health=False)
    item = next(item for item in items if item.workspace_id == target.workspace_id)
    assert item.connection_error == expected_record
    assert native_location not in item.model_dump_json()


@pytest.mark.asyncio
async def test_runtime_controller_rejects_restart_for_external_backend(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "gateway.json")
    registry.upsert(WorkspaceTarget(gateway_id=GATEWAY_ID,
        workspace_id=UUID_A, name="External", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
         managed=False,
    ))
    controller = GatewayWorkspaceRuntimeController(registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs")
    with pytest.raises(ValueError, match="Gateway 托管"):
        await controller.safe_restart_managed_backend(UUID_A, request_id="req")


def test_runtime_controller_updates_health_controller_config(tmp_path: Path) -> None:
    controller = GatewayWorkspaceRuntimeController(
        registry=GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "gateway.json"),
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
    stopped = await controller.stop_managed_backend("00000000-0000-4000-8000-000000000001", request_id="stop", group_request=graph.group_request)
    assert stopped.status == "stopped"
    assert not any(registry.has_runtime(member_id) for member_id in graph.owner.contract.workspace_ids)
    assert graph.owner.process is None
    started = await controller.start_managed_backend("00000000-0000-4000-8000-000000000002", request_id="start", group_request=graph.group_request)
    assert started.status == "started"
    assert len(process_boundary.starts) == 1
    assert all(registry.has_runtime(member_id) for member_id in graph.owner.contract.workspace_ids)
    registry.resolve("00000000-0000-4000-8000-000000000001").system_default = True
    with pytest.raises(PermissionError, match="默认工作区不能关闭"):
        await controller.stop_managed_backend("00000000-0000-4000-8000-000000000002", request_id="default", group_request=graph.group_request)


@pytest.mark.asyncio
@pytest.mark.parametrize("exited", [False, True])
async def test_failed_stop_only_rechecks_health_while_original_backend_is_running(
    workspace_runtime_controller_case, monkeypatch: pytest.MonkeyPatch, exited: bool,
) -> None:
    case = workspace_runtime_controller_case
    owner = case.graph.owner
    process = owner.process
    assert isinstance(process, ProcessProbe)
    original_close = process.close
    original_error = TimeoutError("原生优雅关闭超时，进程可能已强制回收")
    verified: list[object] = []

    def fail_close(**kwargs: object) -> None:
        if exited:
            original_close()
        raise original_error

    async def verify(contract, **kwargs):
        verified.append(contract)
        assert process.is_alive(), "已退出后端不得进入120秒健康恢复等待"
        return process.pid

    with monkeypatch.context() as patch:
        patch.setattr(process, "close", fail_close)
        patch.setattr("app.gateway.runtime.group_actions.verify_managed_backend_group", verify)
        with pytest.raises(TimeoutError) as raised:
            await case.controller.stop_managed_backend(
                UUID_A, request_id="stop-after-native-timeout", group_request=case.graph.group_request,
            )

    assert raised.value is original_error
    assert verified == ([] if exited else [owner.contract])
    assert owner.process is process
    assert (owner.error is not None) is exited
    assert all(case.registry.has_runtime(member_id) for member_id in owner.contract.workspace_ids)
    assert all(handle.is_alive() for runtime in case.graph.runtimes.values() for handle in runtime.processes.values())
    if exited:
        assert not any(path.endswith("/drain/cancel") for _, path in case.runtime_actions)
    else:
        assert all(case.lifecycle_states[member_id] == "ready" for member_id in owner.contract.workspace_ids)


@pytest.mark.asyncio
async def test_runtime_controller_blocks_stop_for_gateway_proxy_stream(workspace_runtime_controller_case) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    lifecycle_states = workspace_runtime_controller_case.lifecycle_states
    registry.acquire_route_reference("00000000-0000-4000-8000-000000000002", streaming=True)
    try:
        blocked = await controller.stop_managed_backend("00000000-0000-4000-8000-000000000001", request_id="stream", group_request=graph.group_request)
        assert blocked.status == "blocked"
        assert [(item.workspace_id, item.kind) for item in blocked.blockers] == [("00000000-0000-4000-8000-000000000002", "proxy_stream")]
        assert all(state == "ready" for state in lifecycle_states.values())
        assert sum(path.endswith("/cancel") for _, path in runtime_actions) == 2
        assert graph.owner.process is not None
    finally:
        registry.release_route_reference("00000000-0000-4000-8000-000000000002", streaming=True)


@pytest.mark.asyncio
async def test_shared_safe_restart_blocks_and_force_interrupts_every_member(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    lifecycle_states = workspace_runtime_controller_case.lifecycle_states
    blockers_by_workspace = workspace_runtime_controller_case.blockers_by_workspace
    blockers_by_workspace["00000000-0000-4000-8000-000000000002"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    blocked = await controller.safe_restart_managed_backend("00000000-0000-4000-8000-000000000001", request_id="safe", group_request=graph.group_request)
    assert blocked.status == "blocked" and blocked.blockers[0].workspace_id == "00000000-0000-4000-8000-000000000002"
    assert not process_boundary.starts
    forced = await controller.force_restart_managed_backend("00000000-0000-4000-8000-000000000001", request_id="force", group_request=graph.group_request)
    assert forced.status == "restarted" and forced.forced
    assert {member_id for member_id, path in runtime_actions if path.endswith("/force")} == {"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}
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
    first = asyncio.create_task(restart("00000000-0000-4000-8000-000000000001"))
    await asyncio.sleep(0.002)
    second = asyncio.create_task(restart("00000000-0000-4000-8000-000000000002"))
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert peak == 1
    assert sum(isinstance(result, ValueError) for result in outcomes) == 1
    assert len(process_boundary.starts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_fails", [False, True])
@pytest.mark.parametrize("native", ["/private-restart/backend", r"C:\private-restart\backend", r"\\server\private-restart\backend"])
async def test_runtime_controller_records_restart_failure_after_old_runtime_recovery(
    workspace_runtime_controller_case, process_boundary, monkeypatch, recovery_fails: bool, native: str,
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
    original = RuntimeError(f"候选启动失败 {native}")
    process_boundary.backend_outcomes[:] = [original, RuntimeError("旧组恢复失败") if recovery_fails else None]
    old = graph.owner.contract
    with pytest.raises(RuntimeError, match="候选启动失败"):
        await controller.safe_restart_managed_backend("00000000-0000-4000-8000-000000000001", request_id="recovery", group_request=graph.group_request)
    assert recorded["old_runtime_recovered"] is not recovery_fails
    assert recorded["candidate_ref"] == "candidate"
    assert recorded["error"] is original
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
        registry=GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "gateway.json"),
        project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    await controller._resolve_pending_restart(WorkspaceTarget(gateway_id=GATEWAY_ID,
        workspace_id=UUID_A, name="Candidate", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:42000", connection_kind="local",
    ), candidate_ref="candidate-ref", request_id="request-id")
    assert captured["GET"].headers["X-BoxTeam-Workspace-Id"] == UUID_A
    assert captured["POST"].url.path == "/api/v1/config/pending/resolve"
    assert captured["POST"].url.params["candidate_ref"] == "candidate-ref"
    assert captured["POST"].headers["X-Request-ID"] == "request-id"


@pytest.mark.asyncio
async def test_candidate_runtime_start_failure_detaches_adopted_and_closes_new_processes(single_workspace_graph, process_boundary, monkeypatch, tmp_path, single_workspace_registry) -> None:
    single_workspace_graph.owner.close()
    error = RuntimeError("候选 health 失败")
    process_boundary.backend_outcomes[:] = [error]
    with pytest.raises(RuntimeError) as raised:
        await start_managed_backend_runtime(
            owner=single_workspace_graph.owner, registry=single_workspace_registry, project_root=tmp_path, log_dir=tmp_path / "logs",
            preserve_adopted_processes_on_failure=True,
        )
    assert raised.value is error
    assert process_boundary.handles[0].closed
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
@pytest.mark.parametrize("http_drain_seconds", [0.25, 2])
async def test_backend_restart_keeps_process_exit_budget_separate_from_http_drain(
    single_workspace_graph, process_boundary, tmp_path, single_workspace_registry,
    monkeypatch: pytest.MonkeyPatch, http_drain_seconds: float,
) -> None:
    owner = single_workspace_graph.owner
    original = owner.process
    waits: list[float] = []

    def finish_process(*, timeout_seconds: float = 8) -> None:
        waits.append(timeout_seconds)
        # Windows实测：lifespan已完成，解释器仍需约2秒才能真正退出。
        if timeout_seconds <= 2:
            raise TimeoutError("解释器退出仍在进行")
        original.alive = False
        original.closed = True

    monkeypatch.setattr(original, "close", finish_process)
    await rebuild_managed_backend_runtime(
        owner=owner, registry=single_workspace_registry, desired_members=owner.contract.members,
        runtimes=single_workspace_graph.runtimes, project_root=tmp_path, log_dir=tmp_path / "logs",
        connection_drain_timeout_seconds=http_drain_seconds,
    )
    assert waits == [8]
    assert original.closed and owner.process is process_boundary.handles[0]
    assert process_boundary.starts[0]["connection_drain_timeout_seconds"] == http_drain_seconds


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
            startup_contracts={"00000000-0000-4000-8000-000000000001": startup} if failure == "config-proof" else None,
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
    assert single_workspace_graph.runtimes["00000000-0000-4000-8000-000000000001"].service_urls["workspace_api"] == old_contract.backend_url


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "backend_id", "missing-backend-id", "generation", "manifest", "pid"])
async def test_gateway_adopts_matching_workspace_backend(workspace_runtime_controller_case, local_http, mismatch, tmp_path, monkeypatch, windows_process_tree_stub) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    contract = graph.owner.contract
    requests = []
    adopted_logs = []
    windows_process_tree_stub.add_process(43212, parent_pid=None, starttime=1.0)
    ownership = SimpleNamespace(process_tree_identity=lambda pid: (pid, 1.0, 1.0))
    logging_state = {
        "service": "backend", "pid": 43212, "runtime_instance_id": "backend-instance",
        "applied_revision": None, "level": "info", "pretty": True, "log_sink": None,
    }
    def adopt_log(store, role, port, proof):
        assert store.root == tmp_path / "logs"
        assert role == "backend" and proof == logging_state
        adopted_logs.append(proof)
        return ownership
    monkeypatch.setattr("app.gateway.runtime.local_workspace.ProcessLogStore.adopt", adopt_log)
    def response(request):
        if request.url.path == "/api/v1/internal/runtime-logging":
            assert request.headers["X-Local-Token"] != "local-dev-token"
            return httpx.Response(200, json={"data": logging_state})
        identity = request.headers["X-BoxTeam-Workspace-Id"]
        requests.append(identity)
        payload = {"workspace_id": identity, "status": "ok", "process_id": 43212,
                   "backend_id": read_backend_id(get_boxteam_home()),
                   "backend_generation": contract.generation, "mount_manifest_digest": mounted_workspace_manifest_digest(registry.resolve_backend_mounts(contract))}
        if mismatch == "backend_id":
            payload["backend_id"] = "00000000-0000-4000-8000-000000000099"
        elif mismatch == "missing-backend-id":
            payload.pop("backend_id")
        elif mismatch == "generation":
            payload["backend_generation"] = "stale"
        elif mismatch == "manifest":
            payload["mount_manifest_digest"] = "wrong"
        elif mismatch == "pid" and identity == UUID_B:
            payload["process_id"] = 43213
        return httpx.Response(200, json=payload)
    local_http(response)
    if mismatch is None:
        adopted = await _adopt_workspace_backend(contract=contract, mounts=registry.resolve_backend_mounts(contract), log_dir=tmp_path / "logs")
        assert adopted.pid == 43212 and requests == [UUID_A, UUID_B]
        assert adopted.log_ownership is ownership
        assert adopted_logs == [logging_state]
    else:
        with pytest.raises(RuntimeError, match="完整组"):
            await _adopt_workspace_backend(contract=contract, mounts=registry.resolve_backend_mounts(contract), log_dir=tmp_path / "logs")
        assert not adopted_logs


@pytest.mark.asyncio
@pytest.mark.parametrize("adopt_backend", [False, True])
async def test_managed_runtime_adopts_or_reclaims_persisted_backend_once(single_workspace_graph, process_boundary, monkeypatch, tmp_path, adopt_backend, single_workspace_registry) -> None:
    single_workspace_graph.owner.close()
    previous = single_workspace_graph.owner.contract
    backend = ProcessProbe("persisted-backend")
    async def adopt_backend_handle(**_):
        return backend
    monkeypatch.setattr(local_workspace, "_adopt_workspace_backend", adopt_backend_handle)
    contract, runtimes = await start_managed_backend_runtime(
        owner=single_workspace_graph.owner, registry=single_workspace_registry, project_root=tmp_path, log_dir=tmp_path / "logs", adopt_existing_backend=adopt_backend,
    )
    assert contract.workspace_ids == ("00000000-0000-4000-8000-000000000001",)
    assert runtimes["00000000-0000-4000-8000-000000000001"].processes == {}
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


@pytest.mark.asyncio
async def test_full_health_proof_requires_all_mounts_same_process(
    workspace_runtime_controller_case,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    assert await verify_managed_backend_group(graph.owner.contract, mounts=registry.resolve_backend_mounts(graph.owner.contract)) == 7000


@pytest.mark.asyncio
async def test_started_backend_confirms_service_only_after_all_mounts(
    workspace_runtime_controller_case, process_boundary, monkeypatch,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    process = ProcessProbe("new")
    observed = []
    health = process_boundary.health
    async def observe(url, workspace_id, *args, **kwargs):
        assert process.confirmed_service_pids == []
        observed.append(workspace_id)
        return await health(url, workspace_id, *args, **kwargs)
    monkeypatch.setattr(local_workspace, "wait_for_workspace_health", observe)
    assert await verify_managed_backend_group(
        graph.owner.contract, mounts=registry.resolve_backend_mounts(graph.owner.contract), process=cast(ManagedProcess, process),
    ) == 7000
    assert observed == [UUID_A, UUID_B]
    assert process.confirmed_service_pids == [7000]


@pytest.mark.asyncio
async def test_inconsistent_mount_pid_does_not_confirm_service(
    workspace_runtime_controller_case, process_boundary, monkeypatch,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    process = ProcessProbe("new")
    health = process_boundary.health
    async def inconsistent(url, workspace_id, *args, **kwargs):
        payload = await health(url, workspace_id, *args, **kwargs)
        return {**payload, "process_id": 7001} if workspace_id == UUID_B else payload
    monkeypatch.setattr(local_workspace, "wait_for_workspace_health", inconsistent)
    with pytest.raises(RuntimeError, match="完整组响应来自不同进程"):
        await verify_managed_backend_group(
            graph.owner.contract, mounts=registry.resolve_backend_mounts(graph.owner.contract), process=cast(ManagedProcess, process),
        )
    assert process.confirmed_service_pids == []


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
            "00000000-0000-4000-8000-000000000001", request_id="force-streams", group_request=graph.group_request,
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
    lease = registry.acquire_route_reference("00000000-0000-4000-8000-000000000002", streaming=True)
    try:
        with pytest.raises(RuntimeError, match="00000000-0000-4000-8000-000000000002.*旧路由"):
            await controller.force_restart_managed_backend(
                "00000000-0000-4000-8000-000000000001", request_id="force-timeout", group_request=graph.group_request,
            )
        assert lease.invalidated.is_set()
        assert graph.owner.process is old_process and not old_process.closed
        assert not process_boundary.starts
        assert all(registry.resolve(member_id).connection_error for member_id in graph.owner.contract.workspace_ids)
        for member_id in graph.owner.contract.workspace_ids:
            with pytest.raises(RuntimeError, match="stage=backend_group_action, error=RuntimeError"):
                registry.resolve_service_url(member_id, "workspace_api")
    finally:
        registry.release_route_reference("00000000-0000-4000-8000-000000000002", streaming=True)
    result = await controller.start_managed_backend(
        "00000000-0000-4000-8000-000000000001", request_id="recover-start", group_request=graph.group_request,
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
            await method("00000000-0000-4000-8000-000000000001", request_id="observer", group_request=graph.group_request)
        assert raised.value is failure
        assert graph.owner.contract is not old_contract
        assert graph.owner.error is None
        assert graph.owner.process is process_boundary.handles[-1]
        assert not graph.owner.process.closed
        assert not graph.owner.unpublished_runtimes
        assert all(registry.managed_runtime(member_id).backend_owner is graph.owner for member_id in graph.owner.contract.workspace_ids)
        persisted = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path)
        assert persisted.backend_owner("00000000-0000-4000-8000-000000000001").contract == graph.owner.contract
        assert any("已提交" in note for note in failure.__notes__)
    finally:
        registry.remove_commit_observer(observer)


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
        blockers_by_workspace["00000000-0000-4000-8000-000000000002"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    request = graph.group_request.model_copy(update={"desired_workspace_ids": []})
    method = controller.force_restart_managed_backend if forced else controller.safe_restart_managed_backend
    result = await method("00000000-0000-4000-8000-000000000001", request_id="empty", group_request=request)
    assert result.status == "stopped" and result.forced is forced
    assert set(result.affected_workspace_ids) == {"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}
    assert old.events.count("close:old") == 1 and old.closed
    assert all(handle.closed for handle in auxiliaries)
    assert not registry.backend_owners() and not process_boundary.starts
    assert not graph.owner.unpublished_runtimes
    for identity in ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"):
        target = registry.resolve(identity)
        assert not target.desired_running and not target.local_service_urls and not target.backend_url
        assert not registry.has_runtime(identity) and registry.backend_owner(identity) is None
    persisted = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path)
    assert not persisted.backend_owners()
    if forced:
        assert {identity for identity, path in runtime_actions if path.endswith("/force")} == {"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}


@pytest.mark.asyncio
async def test_initiator_can_exit_while_sibling_keeps_unique_owner(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    old = graph.owner.process
    exiting = list(graph.runtimes["00000000-0000-4000-8000-000000000001"].processes.values())
    kept = dict(graph.runtimes["00000000-0000-4000-8000-000000000002"].processes)
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]})
    result = await controller.safe_restart_managed_backend("00000000-0000-4000-8000-000000000001", request_id="exit-initiator", group_request=request)
    assert result.status == "restarted" and old.closed and len(process_boundary.starts) == 1
    assert graph.owner.contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",)
    assert registry.backend_owner("00000000-0000-4000-8000-000000000001") is None
    assert registry.backend_owner("00000000-0000-4000-8000-000000000002") is graph.owner
    assert all(handle.closed for handle in exiting)
    assert graph.runtimes["00000000-0000-4000-8000-000000000002"].processes == kept and all(not handle.closed for handle in kept.values())


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_default_exit_and_duplicate_mount_identity_reject_before_drain(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    registry.resolve("00000000-0000-4000-8000-000000000002").system_default = True
    with pytest.raises(PermissionError, match="默认工作区"):
        await controller.safe_restart_managed_backend(
            "00000000-0000-4000-8000-000000000001", request_id="default-exit", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
        )
    registry.resolve("00000000-0000-4000-8000-000000000002").system_default = False
    with pytest.raises(ValueError, match="重复身份"):
        await controller.safe_restart_managed_backend(
            "00000000-0000-4000-8000-000000000001", request_id="duplicate", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": [UUID_A, UUID_A]}),
        )
    assert not runtime_actions and not process_boundary.starts and not graph.owner.process.closed


@pytest.mark.asyncio
async def test_controlled_delete_requires_group_confirmation_and_preserves_blocked_member(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    blockers_by_workspace = workspace_runtime_controller_case.blockers_by_workspace
    blocked = await controller.remove_managed_workspace("00000000-0000-4000-8000-000000000001", request_id="missing-contract")
    assert blocked.status == "blocked" and blocked.blockers[0].kind == "backend_group"
    assert not runtime_actions and not graph.owner.process.closed
    blockers_by_workspace["00000000-0000-4000-8000-000000000002"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]})
    blocked = await controller.remove_managed_workspace("00000000-0000-4000-8000-000000000001", request_id="safe-delete", group_request=request)
    assert blocked.status == "blocked" and registry.resolve("00000000-0000-4000-8000-000000000001").desired_running
    blockers_by_workspace["00000000-0000-4000-8000-000000000002"] = []
    result = await controller.remove_managed_workspace("00000000-0000-4000-8000-000000000001", request_id="delete", group_request=request)
    assert result.status == "restarted" and graph.owner.contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",)
    with pytest.raises(LookupError):
        registry.resolve("00000000-0000-4000-8000-000000000001")
    assert len(process_boundary.starts) == 1


@pytest.mark.asyncio
async def test_force_exit_failed_durable_ack_never_closes_owner(workspace_runtime_controller_case, process_boundary, monkeypatch) -> None:
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    old = graph.owner.process
    action = controller._runtime_action
    async def fail_second(target, path, **kwargs):
        if target.workspace_id == "00000000-0000-4000-8000-000000000002" and path.endswith("/force"):
            runtime_actions.append((target.workspace_id, path))
            raise RuntimeError("00000000-0000-4000-8000-000000000002 durable interrupted 提交失败")
        return await action(target, path, **kwargs)
    monkeypatch.setattr(controller, "_runtime_action", fail_second)
    with pytest.raises(RuntimeError, match="durable interrupted"):
        await controller.force_restart_managed_backend(
            "00000000-0000-4000-8000-000000000001", request_id="failed-ack", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
        )
    assert graph.owner.process is old and not old.closed and not process_boundary.starts
    assert {identity for identity, path in runtime_actions if path.endswith("/force")} == {"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["publication"])
async def test_exit_failure_recovers_complete_old_group(workspace_runtime_controller_case, process_boundary, monkeypatch, failure) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    old_contract = graph.owner.contract
    original = registry._save
    def fail_exit(**kwargs):
        if kwargs.get("backend_contracts") == ():
            raise RuntimeError("exit publication CAS 失败")
        return original(**kwargs)
    monkeypatch.setattr(registry, "_save", fail_exit)
    with pytest.raises(RuntimeError, match="exit"):
        await controller.safe_restart_managed_backend(
            "00000000-0000-4000-8000-000000000001", request_id="recover-exit", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
        )
    assert graph.owner.contract == old_contract and graph.owner.error is None
    assert len(process_boundary.starts) == 1 and graph.owner.process is process_boundary.handles[-1]
    assert all(registry.backend_owner(identity) is graph.owner and registry.resolve(identity).desired_running for identity in old_contract.workspace_ids)
    assert GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path).backend_owner("00000000-0000-4000-8000-000000000001").contract == old_contract


@pytest.mark.asyncio
async def test_exit_after_stop_releases_contract_without_backend_start(workspace_runtime_controller_case, process_boundary) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    await controller.stop_managed_backend("00000000-0000-4000-8000-000000000001", request_id="stop", group_request=graph.group_request)
    result = await controller.safe_restart_managed_backend(
        "00000000-0000-4000-8000-000000000001", request_id="exit-stopped", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
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
                "00000000-0000-4000-8000-000000000001", request_id="exit-observer", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": []}),
            )
        assert raised.value is failure and not registry.backend_owners() and not process_boundary.starts
        assert graph.owner.process is None and not graph.owner.unpublished_runtimes
        assert not GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path).backend_owners()
    finally:
        registry.remove_commit_observer(observer)


def test_registry_cannot_bypass_controlled_managed_exit(workspace_runtime_controller_case) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    with pytest.raises(ValueError, match="受控退出"):
        registry.remove("00000000-0000-4000-8000-000000000001")
    with pytest.raises(ValueError, match="完整组"):
        registry.upsert(replace(registry.resolve("00000000-0000-4000-8000-000000000001")), runtime=WorkspaceRuntime(service_urls={}))
    assert registry.backend_owner("00000000-0000-4000-8000-000000000001") is graph.owner and not graph.owner.process.closed


@pytest.fixture
def deletion_http_application(workspace_runtime_controller_case, tmp_path, monkeypatch):
    registry = workspace_runtime_controller_case.registry
    controller = workspace_runtime_controller_case.controller
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, gateway_http_exception_handler)
    app.state.registry = registry
    app.state.gateway_id = registry.gateway_id
    app.state.workspace_runtime_controller = controller
    app.state.port_forward_manager = SshPortForwardManager(
        registry=registry, storage_path=tmp_path / "forwards.json", log_dir=tmp_path / "logs",
    )
    app.dependency_overrides[verify_gateway_token] = lambda: "local-test-token"
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
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
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}).model_dump(mode="json")
    blockers_by_workspace["00000000-0000-4000-8000-000000000002"] = [{"kind": "job", "resource_id": "job-b", "session_id": "session-b", "status": "running"}]
    before = graph.owner.contract
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", f"{prefix}/00000000-0000-4000-8000-000000000001", json=request, headers={"X-Request-ID": "delete-blocked"})
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["status"] == "blocked" and detail["backend_generation"] == before.generation
        assert set(detail["affected_workspace_ids"]) == {"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}
        assert detail["blockers"][0]["workspace_id"] == "00000000-0000-4000-8000-000000000002"
        assert detail["blockers"][0]["resource_id"] == "job-b"
        assert {item["workspace_id"] for item in detail["workspaces"]["items"]} == {"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}
        assert response.headers["X-Request-ID"] == "delete-blocked"
        assert registry.backend_owner("00000000-0000-4000-8000-000000000001") is graph.owner and graph.owner.contract is before
        assert registry.resolve("00000000-0000-4000-8000-000000000001").desired_running and not process_boundary.starts
        response = await client.request("DELETE", f"{prefix}/00000000-0000-4000-8000-000000000001?force=true", json=request, headers={"X-Request-ID": "delete-forced"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert response.headers["X-Request-ID"] == result["request_id"] == "delete-forced"
        assert [item["workspace_id"] for item in result["data"]["items"]] == ["00000000-0000-4000-8000-000000000002"]
    assert graph.owner.contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",) and len(process_boundary.starts) == 1
    assert {identity for identity, path in runtime_actions if path.endswith("/force")} == {"00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"}
    reopened = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store)
    assert reopened.active_workspace == (GATEWAY_ID, UUID_B) and reopened.backend_owner("00000000-0000-4000-8000-000000000002").contract == graph.owner.contract
    assert {item["workspace_id"] for item in registry._state_store.load_workspace_registry()["targets"]} == {"00000000-0000-4000-8000-000000000002"}
    with pytest.raises(LookupError):
        reopened.resolve("00000000-0000-4000-8000-000000000001")
    with pytest.raises(LookupError):
        registry.resolve("00000000-0000-4000-8000-000000000001")


@pytest.mark.asyncio
async def test_delete_rejects_unrelated_desired_set_before_any_drain(workspace_runtime_controller_case, deletion_http_application) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    request = graph.group_request.model_copy(update={"desired_workspace_ids": []}).model_dump(mode="json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", "/api/gateway/workspaces/00000000-0000-4000-8000-000000000001", json=request)
    assert response.status_code == 409
    assert response.json()["detail"] == "工作区删除失败: stage=workspace_remove, error=ValueError"
    assert not runtime_actions and not graph.owner.process.closed and registry.resolve("00000000-0000-4000-8000-000000000001").desired_running


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
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}).model_dump(mode="json")
    action = controller._runtime_action
    async def failed_ack(target, path, **kwargs):
        if target.workspace_id == "00000000-0000-4000-8000-000000000002" and path.endswith("/force"):
            raise httpx.ConnectError("00000000-0000-4000-8000-000000000002 durable ack 连接断开")
        return await action(target, path, **kwargs)
    monkeypatch.setattr(controller, "_runtime_action", failed_ack)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", f"{prefix}/00000000-0000-4000-8000-000000000001?force=true", json=request)
    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "工作区删除失败: stage=workspace_remove, error=ConnectError"
    assert graph.owner.process is old and not old.closed and not process_boundary.starts
    assert registry.backend_owner("00000000-0000-4000-8000-000000000001") is graph.owner and registry.resolve("00000000-0000-4000-8000-000000000001").desired_running


@pytest.fixture
async def stopped_workspace_runtime_controller_case(workspace_runtime_controller_case, process_boundary):
    graph = workspace_runtime_controller_case.graph
    controller = workspace_runtime_controller_case.controller
    runtime_actions = workspace_runtime_controller_case.runtime_actions
    previous = graph.owner.contract
    await controller.stop_managed_backend("00000000-0000-4000-8000-000000000001", request_id="stopped-fixture", group_request=graph.group_request)
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
    request = stale.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}).model_dump(mode="json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("DELETE", f"{prefix}/00000000-0000-4000-8000-000000000001?force={str(forced).lower()}", json=request)
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert [item["workspace_id"] for item in items] == ["00000000-0000-4000-8000-000000000002"]
    assert items[0]["status"] == "offline"
    # 受管/联邦列表保留各自既有精简 DTO；完整生命周期投影取同一 registry 权威状态。
    remaining = next(item.model_dump(mode="json") for item in await GatewayWorkspaceRegistry.list_dtos(registry, check_health=False) if item.workspace_id == "00000000-0000-4000-8000-000000000002")
    assert remaining["status"] == "offline" and remaining["affected_workspace_ids"] == ["00000000-0000-4000-8000-000000000002"]
    assert all(service["local_url"] is None for service in remaining["services"].values())
    if prefix == "/api/gateway/workspaces":
        assert items[0]["backend_generation"] == remaining["backend_generation"]
        assert items[0]["affected_workspace_ids"] == remaining["affected_workspace_ids"]
        assert items[0]["services"] == remaining["services"]
    contract = graph.owner.contract
    assert contract.owner_id == previous.owner_id and contract.generation != previous.generation
    assert contract.members == (previous.members[1],) and remaining["backend_generation"] == contract.generation
    assert graph.owner.process is None and not process_boundary.starts and not runtime_actions
    assert not registry.has_runtime("00000000-0000-4000-8000-000000000002") and not registry.resolve("00000000-0000-4000-8000-000000000002").desired_running
    assert registry.resolve("00000000-0000-4000-8000-000000000002").runtime_lease_id is None
    restored = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store)
    assert restored.backend_owner("00000000-0000-4000-8000-000000000002").contract == contract
    assert not restored.resolve("00000000-0000-4000-8000-000000000002").desired_running and not restored.has_runtime("00000000-0000-4000-8000-000000000002")
    with pytest.raises(LookupError):
        registry.resolve("00000000-0000-4000-8000-000000000001")
    with pytest.raises(ValueError, match="陈旧"):
        await controller.start_managed_backend("00000000-0000-4000-8000-000000000002", request_id="stale", group_request=stale)
    started = await controller.start_managed_backend("00000000-0000-4000-8000-000000000002", request_id="remaining-start", group_request=graph.group_request)
    assert started.status == "started" and len(process_boundary.starts) == 1
    mounts = process_boundary.starts[0]["mounts"]
    assert [(mount.workspace_id, mount.workspace_root) for mount in mounts] == [(UUID_B, registry.local_workspace_root(UUID_B))]
    assert graph.owner.contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",)
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
        "00000000-0000-4000-8000-000000000001", request_id="offline-exit", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}),
    )
    assert result.status == "stopped" and result.affected_workspace_ids == ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
    assert graph.owner.contract.generation != previous.generation and graph.owner.contract.members == (previous.members[1],)
    assert registry.backend_owner("00000000-0000-4000-8000-000000000001") is None and registry.backend_owner("00000000-0000-4000-8000-000000000002") is graph.owner
    assert registry.resolve("00000000-0000-4000-8000-000000000001").backend_url == "" and not registry.resolve("00000000-0000-4000-8000-000000000001").local_service_urls
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
        "00000000-0000-4000-8000-000000000001", request_id="same-offline-set", group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000001"]}),
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
    target = registry.resolve("00000000-0000-4000-8000-000000000001")
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]})
    exception, message = ValueError, "陈旧"
    if invalid == "generation":
        request = request.model_copy(update={"expected_backend_generation": "stale"})
    elif invalid == "current":
        request = request.model_copy(update={"expected_workspace_ids": ["00000000-0000-4000-8000-000000000002"]})
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
        process_boundary.identities[Path(target.root_path)] = UUID_B
        exception, message = RuntimeError, "Backend UUID 不匹配"
    else:
        monkeypatch.setattr(target, "root_path", str(Path(target.root_path) / "changed"))
        exception, message = FileNotFoundError, "工作区目录不存在"
    with pytest.raises(exception, match=message):
        await controller.remove_managed_workspace("00000000-0000-4000-8000-000000000001", request_id="offline-invalid", group_request=request)
    assert graph.owner.contract is previous and registry.resolve("00000000-0000-4000-8000-000000000001") is target
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
    request = graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]})
    streams = {"00000000-0000-4000-8000-000000000001": False, "00000000-0000-4000-8000-000000000002": True}
    leases = {identity: registry.acquire_route_reference(identity, streaming=streaming) for identity, streaming in streams.items()}
    async def release_invalidated() -> None:
        await asyncio.gather(*(lease.invalidated.wait() for lease in leases.values()))
        assert graph.owner.contract is previous and not runtime_actions and not process_boundary.starts
        for identity, streaming in streams.items():
            registry.release_route_reference(identity, streaming=streaming)
    release = None
    try:
        blocked = await controller.remove_managed_workspace("00000000-0000-4000-8000-000000000001", request_id="offline-blocked", group_request=request)
        assert blocked.status == "blocked" and {(item.workspace_id, item.kind) for item in blocked.blockers} == {("00000000-0000-4000-8000-000000000001", "proxy_request"), ("00000000-0000-4000-8000-000000000002", "proxy_stream")}
        assert graph.owner.contract is previous and not any(lease.invalidated.is_set() for lease in leases.values())
        release = asyncio.create_task(release_invalidated())
        result = await controller.remove_managed_workspace("00000000-0000-4000-8000-000000000001", request_id="offline-force", force=True, group_request=request)
        await release
        assert result.status == "stopped" and result.forced
        assert {item.workspace_id for item in result.blockers} == set(streams)
        assert graph.owner.contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",) and graph.owner.process is None
        assert not process_boundary.starts and not runtime_actions
    finally:
        if release is not None and not release.done():
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)
        for identity, streaming in streams.items():
            if registry._route_reference_counts.get((registry.gateway_id, identity), (0, 0))[int(streaming)]:
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
    lease = registry.acquire_route_reference("00000000-0000-4000-8000-000000000002", streaming=True)
    try:
        with pytest.raises(RuntimeError, match="旧路由引用未释放"):
            await controller.remove_managed_workspace(
                "00000000-0000-4000-8000-000000000001", request_id="offline-route-timeout", force=True,
                group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}),
            )
        assert lease.invalidated.is_set() and graph.owner.contract is previous
        assert registry.resolve("00000000-0000-4000-8000-000000000001").connection_error and not registry.resolve("00000000-0000-4000-8000-000000000001").desired_running
        assert not runtime_actions and not process_boundary.starts and graph.owner.process is None
        assert GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store).backend_owner("00000000-0000-4000-8000-000000000001").contract == previous
    finally:
        registry.release_route_reference("00000000-0000-4000-8000-000000000002", streaming=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("native_location", ["/private/group-failure", r"C:\private\group-failure", r"\\server\private\group-failure"])
@pytest.mark.parametrize("sink_fails", [False, True])
@pytest.mark.parametrize(("fault", "path"), [
    ("start", "/api/gateway/workspaces/00000000-0000-4000-8000-000000000001/runtime/start"),
    ("restart", "/api/gateway/workspaces/00000000-0000-4000-8000-000000000001/runtime/restart-safe"),
    ("restart", "/api/gateway/workspaces/00000000-0000-4000-8000-000000000001/runtime/restart-force"),
    ("offline-exit", "/api/gateway/workspaces/00000000-0000-4000-8000-000000000001"),
    ("offline-exit", "/api/gateway/managed-workspaces/00000000-0000-4000-8000-000000000001"),
    ("offline-exit", "/api/gateway/federation/managed-workspaces/00000000-0000-4000-8000-000000000001"),
])
async def test_group_failure_facts_reach_http_sqlite_and_original_retry(
    workspace_runtime_controller_case: WorkspaceRuntimeControllerCase,
    deletion_http_application: FastAPI, process_boundary: ProcessBoundary,
    controller_registry_store: GatewayStateStore, monkeypatch: pytest.MonkeyPatch,
    native_location: str, sink_fails: bool, fault: str, path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = workspace_runtime_controller_case.registry
    graph = workspace_runtime_controller_case.graph
    previous = graph.owner.contract
    primary = RuntimeError(f"group primary failed: {native_location}")
    recovery = RuntimeError(f"group recovery failed: {native_location}")
    sink = OSError(5, "diagnostic sink failed", native_location)
    request = graph.group_request.model_dump(mode="json")
    original_save = registry._save
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        if fault in {"start", "offline-exit"}:
            stopped = await client.post("/api/gateway/workspaces/00000000-0000-4000-8000-000000000001/runtime/stop", json=request)
            assert stopped.status_code == 200, stopped.text
        if fault == "start":
            process_boundary.backend_outcomes[:] = [primary]
        elif fault == "restart":
            process_boundary.backend_outcomes[:] = [primary, recovery]
        else:
            request["desired_workspace_ids"] = ["00000000-0000-4000-8000-000000000002"]

            def reject_candidate(**options):
                contracts = options.get("backend_contracts")
                if contracts is not None and any(contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",) for contract in contracts):
                    raise primary
                return original_save(**options)

            monkeypatch.setattr(registry, "_save", reject_candidate)
        if sink_fails:
            def reject_log(*_args: object, **_kwargs: object) -> None:
                raise sink

            monkeypatch.setattr(failures.logger, "error", reject_log)
        response = await client.request(
            "DELETE" if fault == "offline-exit" else "POST", path, json=request,
            headers={"X-Request-ID": "group-request-01"},
        )
        assert response.status_code == 502, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "group-request-01"
        assert "group-failure" not in response.text
        assert "error=RuntimeError" in response.json()["detail"]
        assert ("diagnostic_sink_error=OSError" in response.json()["detail"]) is sink_fails
        assert graph.owner.contract is previous and graph.owner.process is None
        assert graph.owner.error and "group-failure" not in graph.owner.error
        assert all(registry.resolve(member_id).connection_error == graph.owner.error for member_id in previous.workspace_ids)
        if fault == "start":
            assert "stage=backend_start" in graph.owner.error
        elif fault == "offline-exit":
            assert "stage=backend_stopped_exit" in graph.owner.error
        else:
            assert "stage=backend_group_action" in graph.owner.error
        if not sink_fails:
            assert any(record.exc_info and record.exc_info[1] is primary for record in caplog.records)
            if fault == "restart":
                assert any(record.exc_info and record.exc_info[1] is recovery for record in caplog.records)
        else:
            assert any(str(sink) in note for note in primary.__notes__)
        if fault == "restart":
            assert any(str(recovery) in note for note in primary.__notes__)
        for member_id in previous.workspace_ids:
            assert registry.backend_owner(member_id) is graph.owner
            assert registry.resolve(member_id).workspace_id == next(member.workspace_id for member in previous.members if member.workspace_id == member_id)
        stored_failure = controller_registry_store.load_workspace_registry()
        assert stored_failure is not None
        stored_targets = stored_failure["targets"]
        assert isinstance(stored_targets, list)
        assert all("group-failure" not in target["connection_error"] for target in stored_targets)
        cold = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=controller_registry_store)
        cold_owner = cold.backend_owner("00000000-0000-4000-8000-000000000001")
        assert cold_owner is not None and cold_owner.contract == previous
        assert all(cold.resolve(member_id).connection_error == graph.owner.error for member_id in previous.workspace_ids)
        assert len(process_boundary.starts) == (2 if fault == "restart" else 1 if fault == "start" else 0)
        assert all(handle.closed for handle in process_boundary.handles)
        # 修复原边界，再从原公开入口重试，不能手工清掉owner.error或写入成功状态。
        monkeypatch.setattr(registry, "_save", original_save)
        process_boundary.backend_outcomes.clear()
        retry = await client.request("DELETE" if fault == "offline-exit" else "POST", path if fault in {"start", "offline-exit"} else "/api/gateway/workspaces/00000000-0000-4000-8000-000000000001/runtime/start", json=request)
        assert retry.status_code == 200, retry.text
        if fault == "offline-exit":
            assert graph.owner.contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",) and not registry.has_target("00000000-0000-4000-8000-000000000001")
            assert not process_boundary.starts
        else:
            assert graph.owner.process is process_boundary.handles[-1]
            assert not process_boundary.handles[-1].closed
            assert graph.owner.contract.workspace_ids == previous.workspace_ids
        assert graph.owner.error is None
        assert all(registry.resolve(member_id).connection_error is None for member_id in graph.owner.contract.workspace_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/gateway/workspaces/{workspace_id}/runtime/start",
    "/api/gateway/workspaces/{workspace_id}/runtime/stop",
    "/api/gateway/workspaces/{workspace_id}/runtime/restart-safe",
    "/api/gateway/workspaces/{workspace_id}/runtime/restart-force",
    "/api/gateway/workspaces/{workspace_id}/probe",
    "/api/gateway/workspaces/{workspace_id}",
    "/api/gateway/managed-workspaces/{workspace_id}",
    "/api/gateway/federation/managed-workspaces/{workspace_id}",
])
@pytest.mark.parametrize("missing", [False, True])
async def test_lifecycle_rejections_keep_status_and_have_no_runtime_side_effects(
    workspace_runtime_controller_case: WorkspaceRuntimeControllerCase,
    deletion_http_application: FastAPI, process_boundary: ProcessBoundary,
    path: str, missing: bool,
) -> None:
    graph = workspace_runtime_controller_case.graph
    previous = graph.owner.contract
    process = graph.owner.process
    request = graph.group_request.model_copy(update={"expected_backend_generation": "stale"})
    endpoint = path.format(workspace_id="00000000-0000-4000-8000-000000000099" if missing else "00000000-0000-4000-8000-000000000001")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.request("POST" if "/runtime/" in path or path.endswith("/probe") else "DELETE", endpoint, json=request.model_dump(mode="json"))
    assert response.status_code == (404 if missing else 409), response.text
    assert f"error={'LookupError' if missing else 'ValueError'}" in response.json()["detail"]
    assert graph.owner.contract is previous and graph.owner.process is process
    assert graph.owner.error is None and not process_boundary.starts
    assert not workspace_runtime_controller_case.runtime_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("controller_registry_store", [True], indirect=True)
@pytest.mark.parametrize("native_location", ["/private/probe-failure", r"C:\private\probe-failure", r"\\server\private\probe-failure"])
@pytest.mark.parametrize("sink_fails", [False, True])
@pytest.mark.parametrize("fault_errno", [5, 13])
async def test_external_probe_failure_keeps_native_diagnostics_and_original_target_retry(
    workspace_runtime_controller_case: WorkspaceRuntimeControllerCase,
    deletion_http_application: FastAPI, controller_registry_store: GatewayStateStore,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    native_location: str, sink_fails: bool, fault_errno: int,
) -> None:
    registry = workspace_runtime_controller_case.registry
    external_root = Path(registry.resolve(UUID_A).root_path).with_name("external")
    external_root.mkdir()
    target = replace(registry.resolve(UUID_A), workspace_id=UUID_C, root_path=str(external_root), managed=False, desired_running=False)
    registry.upsert(target, activate=False)
    failure = OSError(fault_errno, "probe failed", native_location)

    async def probe(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(runtime_controller_module, "resolve_backend_workspace_id", lambda *_args, **_kwargs: UUID_C)
    monkeypatch.setattr(runtime_controller_module, "wait_for_workspace_health", probe)
    if sink_fails:
        def fail_log(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError(f"sink failed: {native_location}")

        monkeypatch.setattr(failures.logger, "error", fail_log)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=deletion_http_application), base_url="http://gateway") as client:
        response = await client.post("/api/gateway/workspaces/00000000-0000-4000-8000-000000000003/probe", headers={"X-Request-ID": "external-probe-01"})
        expected = f"工作区探测失败: stage=workspace_probe, error={type(failure).__name__}, errno={fault_errno}"
        if sink_fails:
            expected += ", diagnostic_sink_error=RuntimeError"
        assert response.status_code == (409 if fault_errno == 13 else 502) and response.json()["detail"] == expected
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "external-probe-01"
        # PermissionError 仍是原409拒绝；502写入失败事实，均不能改成托管目标。
        assert target.connection_error == (None if fault_errno == 13 else expected)
        assert registry.resolve(UUID_C) is target
        assert target.workspace_id == UUID_C and not target.managed
        stored = controller_registry_store.load_workspace_registry()
        assert stored is not None
        assert "probe-failure" not in str(stored)
        cold = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=controller_registry_store)
        assert cold.resolve(UUID_C).connection_error == target.connection_error
        if sink_fails:
            assert any("sink failed" in note for note in failure.__notes__)
        else:
            assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)

        async def healthy(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(runtime_controller_module, "wait_for_workspace_health", healthy)
        retry = await client.post("/api/gateway/workspaces/00000000-0000-4000-8000-000000000003/probe")
        assert retry.status_code == 200 and target.connection_error is None


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
                "00000000-0000-4000-8000-000000000001", request_id="offline-observer",
                group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}),
            )
        assert raised.value is failure and graph.owner.contract.generation != previous.generation
        assert graph.owner.contract.members == (previous.members[1],) and graph.owner.process is None and graph.owner.error is None
        assert not registry.resolve("00000000-0000-4000-8000-000000000002").desired_running and registry.resolve("00000000-0000-4000-8000-000000000002").runtime_lease_id is None
        with pytest.raises(LookupError):
            registry.resolve("00000000-0000-4000-8000-000000000001")
        restored = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store)
        assert restored.backend_owner("00000000-0000-4000-8000-000000000002").contract == graph.owner.contract
        with pytest.raises(LookupError):
            restored.resolve("00000000-0000-4000-8000-000000000001")
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
        graph.owner.unpublished_runtimes["00000000-0000-4000-8000-000000000001"] = WorkspaceRuntime(service_urls={})
    try:
        exception = ValueError if boundary == "online_incomplete" else RuntimeError
        message = "完整成员 runtime" if boundary == "online_incomplete" else "受控关闭"
        with pytest.raises(exception, match=message):
            registry.publish_backend_runtime(graph.owner, previous, {} if boundary == "online_incomplete" else None)
        assert graph.owner.contract is previous and not registry.resolve("00000000-0000-4000-8000-000000000001").desired_running
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
        await invoke("00000000-0000-4000-8000-000000000001", request_id="ordinary-stopped", group_request=graph.group_request)
    assert graph.owner.contract is previous and graph.owner.error is None and graph.owner.process is None
    assert registry.backend_owner("00000000-0000-4000-8000-000000000001") is graph.owner and not process_boundary.starts and not runtime_actions


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
    other = WorkspaceTarget(gateway_id=GATEWAY_ID,
        workspace_id='00000000-0000-4000-8000-000000000003', name="Other owner", root_path=str(other_root),
        backend_url="http://127.0.0.1:41001", connection_kind="local", managed=True,
         owner="config", parent_workspace=(GATEWAY_ID, '00000000-0000-4000-8000-000000000001'),
    )
    registry.upsert(other, activate=False)
    other_graph = managed_backend_factory((other,), backend_url=other.backend_url, process=ProcessProbe("other-backend"))
    other_graph.publish(registry)
    previous_other = registry.resolve(other.workspace_id)
    result = await controller.remove_managed_workspace(
        "00000000-0000-4000-8000-000000000001", request_id="sqlite-exact-delete",
        group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}),
    )
    assert result.status == "stopped" and graph.owner.contract.workspace_ids == ("00000000-0000-4000-8000-000000000002",)
    assert registry.resolve(other.workspace_id) == replace(previous_other, parent_workspace=None)
    assert registry.backend_owner(other.workspace_id) is other_graph.owner
    assert registry.managed_runtime(other.workspace_id) is other_graph.runtimes[other.workspace_id]
    assert other_graph.owner.process.is_alive() and not other_graph.owner.process.closed
    saved = registry._state_store.load_workspace_registry()
    assert {target["workspace_id"] for target in saved["targets"]} == {"00000000-0000-4000-8000-000000000002", other.workspace_id}
    assert next(target for target in saved["targets"] if target["workspace_id"] == other.workspace_id)["parent_workspace"] is None
    assert len(saved["managed_backends"]) == 2
    reopened = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store)
    assert reopened.resolve(other.workspace_id) == registry.resolve(other.workspace_id)
    assert reopened.backend_owner(other.workspace_id).contract == other_graph.owner.contract
    assert reopened.backend_owner("00000000-0000-4000-8000-000000000002").contract == graph.owner.contract
    with pytest.raises(LookupError):
        reopened.resolve("00000000-0000-4000-8000-000000000001")
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
    writer = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store)
    writer.rename("00000000-0000-4000-8000-000000000002", "Concurrent rename")
    committed = registry._state_store.load_workspace_registry()
    with pytest.raises(RuntimeError, match="CAS") as raised:
        await controller.remove_managed_workspace(
            "00000000-0000-4000-8000-000000000001", request_id="sqlite-real-cas",
            group_request=graph.group_request.model_copy(update={"desired_workspace_ids": ["00000000-0000-4000-8000-000000000002"]}),
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
    with pytest.raises(AttributeError, match="workspace_id"):
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
        path += "/00000000-0000-4000-8000-000000000001"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://target") as client:
        response = await client.request(method, path, headers=headers, json={"root_path": str(root), "create_directory": True} if method == "POST" else None)
    assert response.status_code == 403
    assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
    assert response.json()["request_id"] == headers["X-Request-ID"]
    assert registry._storage_path.read_bytes() == before and registry.registry_revision == revision
    assert graph.owner.contract is contract and graph.owner.process is process and not process.closed
    assert not runtime_actions and not process_boundary.starts and not root.exists()
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
        blocked = await client.delete("/api/gateway/federation/managed-workspaces/00000000-0000-4000-8000-000000000001", headers=headers)
        assert blocked.status_code == 409
        before = registry._storage_path.read_bytes()
        policy.publish({"rules": []})
        denied = await client.delete("/api/gateway/federation/managed-workspaces/00000000-0000-4000-8000-000000000001", headers=headers)
    assert denied.status_code == 403 and registry._storage_path.read_bytes() == before
    assert not runtime_actions and not graph.owner.process.closed and not process_boundary.starts


@pytest.mark.parametrize("action", ["restart-safe", "restart-force"])
@pytest.mark.parametrize("capabilities", [[], ["configuration_read", "configuration_write"]])
async def test_federation_backend_restart_requires_original_admin_before_controller(
    workspace_runtime_controller_case, paired_deletion_http_application, process_boundary, action, capabilities,
) -> None:
    case = workspace_runtime_controller_case
    app, headers = paired_deletion_http_application
    app.dependency_overrides.pop(verify_gateway_access)
    app.state.federation_policy_store.publish({"rules": [
        {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway("peer")}
        for capability in capabilities
    ]})
    process = case.graph.owner.process
    contract = case.graph.owner.contract
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://target") as client:
        response = await client.post(
            f"/api/gateway/workspaces/{UUID_A}/runtime/{action}", headers=headers,
            json=case.graph.group_request.model_dump(mode="json"),
        )
    assert response.status_code == 403 and "managed_workspace_admin" in response.json()["detail"]
    assert case.graph.owner.process is process and case.graph.owner.contract is contract and not process.closed
    assert not case.runtime_actions and not process_boundary.starts


@pytest.mark.parametrize("action", ["restart-safe", "restart-force"])
@pytest.mark.parametrize("recovery", [False, True])
async def test_pending_candidate_write_is_checked_in_original_status_chain_before_retry_or_drain(
    workspace_runtime_controller_case, paired_deletion_http_application, process_boundary, monkeypatch, action, recovery,
) -> None:
    case = workspace_runtime_controller_case
    app, headers = paired_deletion_http_application
    app.dependency_overrides.pop(verify_gateway_access)
    controller = case.controller
    monkeypatch.setattr(controller, "_pending_startup_contract", GatewayWorkspaceRuntimeController._pending_startup_contract.__get__(controller))
    app.state.federation_policy_store.publish({"rules": [
        {"capability": "managed_workspace_admin", "effect": "allow", "principal_ref": principal_ref_for_gateway("peer")},
    ]})
    requests: list[httpx.Request] = []
    client_type = httpx.AsyncClient

    def backend_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/config/reload-status"
        return httpx.Response(200, json={"data": {
            "state": "recovery_required" if recovery else "pending", "restart_required": True, "candidate_ref": "candidate",
        }})

    monkeypatch.setattr(runtime_controller_module.httpx, "AsyncClient", lambda **options: client_type(
        **options, transport=httpx.MockTransport(backend_response),
    ))
    process = case.graph.owner.process
    contract = case.graph.owner.contract
    async with client_type(transport=httpx.ASGITransport(app=app), base_url="http://target") as client:
        response = await client.post(
            f"/api/gateway/workspaces/{UUID_A}/runtime/{action}", headers=headers,
            json=case.graph.group_request.model_dump(mode="json"),
        )
    assert response.status_code == 403 and "configuration_write" in response.json()["detail"]
    assert [(request.method, request.url.path) for request in requests] == [("GET", "/api/v1/config/reload-status")]
    assert case.graph.owner.process is process and case.graph.owner.contract is contract and not process.closed
    assert not case.runtime_actions and not process_boundary.starts
