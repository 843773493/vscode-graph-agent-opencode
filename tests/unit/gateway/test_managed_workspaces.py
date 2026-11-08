import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.config import GatewayConfig
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import request_remote_gateway_management
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    remove_direct_managed_workspace,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import federation, workspaces_lifecycle, workspaces_managed
from app.gateway.routes._shared import get_registry, get_workspace_runtime_controller
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.managed_backend import ManagedBackendOwner
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.port_forwarding import get_port_forward_manager
from tests.support.gateway_managed_backends import ProcessProbe

GATEWAY_ID = "gateway_00000000000040008000000000000001"


@dataclass
class CreationBoundary:
    error: BaseException | None = None
    fail_validation: bool = False
    processes: list[ProcessProbe] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)


@pytest.fixture
def creation_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """只替换工作区身份 CLI 和进程启动，保留原创建、发布及失败结算。"""
    boundary = CreationBoundary()
    backend_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("app.gateway.registry.runtime.resolve_backend_workspace_id", lambda *_, **__: backend_id)
    monkeypatch.setattr("app.gateway.runtime.group_actions.resolve_backend_workspace_id", lambda *_, **__: backend_id)

    async def validate(*_):
        if boundary.fail_validation and boundary.error is not None:
            raise boundary.error

    async def start(*, owner: ManagedBackendOwner, **_):
        process = ProcessProbe("backend")
        boundary.processes.append(process)
        owner.set_process(process)
        member = owner.contract.members[0]
        runtime = WorkspaceRuntime(
            service_urls={"browser_manager": "http://127.0.0.1:42002"},
            backend_owner=owner, workspace_id=backend_id,
        )
        owner.unpublished_runtimes[member.workspace_id] = runtime
        if boundary.error is not None:
            raise boundary.error
        return replace(owner.contract, backend_url="http://127.0.0.1:42000"), {member.workspace_id: runtime}

    original_start = GatewayWorkspaceRuntimeController.start_managed_backend

    async def record_request(controller, workspace_id, *, request_id, group_request=None):
        boundary.request_ids.append(request_id)
        return await original_start(controller, workspace_id, request_id=request_id, group_request=group_request)

    monkeypatch.setattr("app.gateway.runtime.group_actions.validate_managed_backend_bindings", validate)
    monkeypatch.setattr("app.gateway.runtime.group_actions.start_managed_backend_runtime", start)
    monkeypatch.setattr(GatewayWorkspaceRuntimeController, "start_managed_backend", record_request)
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    return boundary


@pytest.fixture
def creation_registry(tmp_path: Path):
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as store:
        yield GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "workspaces.json", state_store=store)


@pytest.fixture
def creation_app(creation_registry: GatewayWorkspaceRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.gateway.main import gateway_http_exception_handler

    application = FastAPI()
    application.state.gateway_id = creation_registry.gateway_id
    application.state.registry = creation_registry
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(gateway_http_exception_handler)
    application.dependency_overrides[verify_gateway_token] = lambda: "local-token"
    application.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    application.dependency_overrides[federation.require_managed_workspace_admin] = lambda: None
    application.dependency_overrides[get_registry] = lambda: creation_registry
    application.dependency_overrides[get_port_forward_manager] = lambda: None
    application.dependency_overrides[get_workspace_runtime_controller] = lambda: GatewayWorkspaceRuntimeController(
        registry=creation_registry, project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    original_list = creation_registry.list_dtos

    async def list_without_network(**_):
        return await original_list(check_health=False)

    monkeypatch.setattr(creation_registry, "list_dtos", list_without_network)
    for module in (workspaces_lifecycle, workspaces_managed, federation):
        application.include_router(module.router)
        monkeypatch.setattr(module, "get_project_root", lambda: tmp_path)
        monkeypatch.setattr(module, "get_runtime_log_root", lambda: tmp_path / "logs")
    return application


@pytest.mark.asyncio
async def test_external_registration_confirms_health_installation_domain_without_rebinding(
    creation_app: FastAPI, creation_registry: GatewayWorkspaceRegistry, creation_boundary: CreationBoundary,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "external"
    root.mkdir()
    creation_app.state.gateway_config = GatewayConfig()
    installation_domain = "00000000-0000-4000-8000-000000000099"
    health_requests: list[str] = []

    async def health(url: str, workspace_id: str, **_):
        health_requests.append(url)
        return {"workspace_id": workspace_id, "backend_id": installation_domain}

    monkeypatch.setattr(workspaces_lifecycle, "wait_for_workspace_health", health)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=creation_app), base_url="http://gateway") as client:
        payload = {"root_path": str(root), "backend_url": "http://127.0.0.1:43001"}
        accepted = await client.post("/api/gateway/workspaces/local", json=payload)
        assert accepted.status_code == 200, accepted.text
        [target] = creation_registry.targets()
        assert target.backend_id == installation_domain and not target.managed
        installation_domain = "00000000-0000-4000-8000-000000000098"
        rejected = await client.post("/api/gateway/workspaces/local", json=payload)
        assert rejected.status_code == 400, rejected.text
        assert creation_registry.resolve(target.key) is target
        assert target.backend_id == "00000000-0000-4000-8000-000000000099"
    assert health_requests == ["http://127.0.0.1:43001/api/v1/health"] * 2
    assert creation_boundary.processes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True], ids=["managed", "external"])
@pytest.mark.parametrize("damage", ["missing", "mismatch", "root_missing"])
async def test_reregister_known_root_never_reinitializes_or_rebinds_identity(
    creation_app: FastAPI, creation_registry: GatewayWorkspaceRegistry,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: bool, damage: str,
) -> None:
    root = tmp_path / "registered"
    root.mkdir()
    original_id = load_or_create_workspace_id(root)
    target = creation_registry.upsert(WorkspaceTarget(gateway_id=GATEWAY_ID,
        workspace_id=original_id, name="原登记",
        root_path=str(root), backend_url="http://127.0.0.1:43001",
         connection_kind="local", managed=False,
    ))
    identity = workspace_identity_path(root)
    if damage in {"missing", "root_missing"}:
        identity.unlink()
        if damage == "root_missing":
            identity.parent.rmdir()
            root.rmdir()
    else:
        identity.write_text('{"workspace_id":"00000000-0000-4000-8000-000000000099"}\n')
    before = identity.read_bytes() if identity.exists() else None
    state = creation_registry._state_store
    assert state is not None
    saved, revision = state.load_workspace_registry(), creation_registry.registry_revision
    later_calls: list[str] = []

    async def unexpected_health(*_args: object, **_kwargs: object) -> None:
        later_calls.append("health")
        raise AssertionError("身份核验失败后不能访问后端")

    async def unexpected_start(*_args: object, **_kwargs: object) -> None:
        later_calls.append("start")
        raise AssertionError("身份核验失败后不能启动后端")

    creation_app.state.gateway_config = GatewayConfig()
    monkeypatch.setattr(workspaces_lifecycle, "get_project_root", Path.cwd)
    monkeypatch.setattr(workspaces_lifecycle, "wait_for_workspace_health", unexpected_health)
    monkeypatch.setattr(GatewayWorkspaceRuntimeController, "start_managed_backend", unexpected_start)
    if external:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=creation_app, raise_app_exceptions=False), base_url="http://gateway",
        ) as client:
            response = await client.post("/api/gateway/workspaces/local", json={
                "root_path": str(root), "backend_url": target.backend_url,
            })
        assert response.status_code == (500 if damage == "missing" else 400), response.text
    else:
        with pytest.raises((RuntimeError, ValueError, FileNotFoundError)):
            await create_direct_managed_workspace(
                registry=creation_registry, project_root=Path.cwd(), log_dir=tmp_path / "logs",
                root_path=str(root), name=None, create_directory=True, request_id="known-root-reregister",
            )
    assert (identity.read_bytes() if identity.exists() else None) == before
    assert state.load_workspace_registry() == saved and creation_registry.registry_revision == revision
    assert creation_registry.resolve(target.workspace_id).workspace_id == original_id
    assert later_calls == []
    assert root.exists() is (damage != "root_missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["workspaces/local", "managed-workspaces", "federation/managed-workspaces"])
@pytest.mark.parametrize("fail_validation", [False, True])
@pytest.mark.parametrize(("error_type", "native_location"), [
    (PermissionError, "/private/create-failure"), (ValueError, r"C:\private\create-failure"),
    (RuntimeError, r"\\server\private\create-failure"),
])
async def test_creation_http_keeps_owner_failure_facts_and_recovers_by_original_start(
    creation_app: FastAPI, creation_registry: GatewayWorkspaceRegistry, creation_boundary: CreationBoundary,
    tmp_path: Path, endpoint: str, error_type: type[Exception], native_location: str, fail_validation: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = creation_registry
    root = tmp_path / "selected"
    root.mkdir()
    primary = error_type(f"startup failed: {native_location}")
    creation_boundary.error = primary
    creation_boundary.fail_validation = fail_validation
    request_id = "req_creation_failure"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=creation_app), base_url="http://gateway") as client:
        response = await client.post(f"/api/gateway/{endpoint}", json={"root_path": str(root)}, headers={"X-Request-ID": request_id})
        expected_status = 400 if error_type is ValueError else (500 if endpoint == "workspaces/local" else 502)
        assert response.status_code == expected_status, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_id
        assert "create-failure" not in response.text and str(root) not in response.text
        target, = registry.targets()
        owner = registry.backend_owner(target.workspace_id)
        assert owner is not None and owner.process is None and not owner.unpublished_runtimes
        expected = f"Backend 启动失败: owner={owner.contract.owner_id}: stage=backend_start, error={error_type.__name__}"
        if error_type is PermissionError:
            expected += ", errno=None"
        assert owner.error == target.connection_error == expected
        assert creation_boundary.request_ids == [request_id]
        if fail_validation:
            assert not creation_boundary.processes
        else:
            assert creation_boundary.processes[0].closed
        assert any(record.exc_info and record.exc_info[1] is primary for record in caplog.records)
        cold = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store)
        assert cold.resolve(target.workspace_id).connection_error == expected
        cold_owner = cold.backend_owner(target.workspace_id)
        assert cold_owner is not None and cold_owner.contract == owner.contract
        generation = owner.contract.generation
        creation_boundary.error = None
        retry = await client.post(f"/api/gateway/workspaces/{target.workspace_id}/runtime/start", headers={"X-Request-ID": "req_creation_retry"})
        assert retry.status_code == 200, retry.text
        assert registry.backend_owner(target.workspace_id) is owner and owner.contract.generation == generation
        assert registry.resolve(target.workspace_id).connection_error is None and owner.error is None
        assert registry.managed_runtime(target.workspace_id).backend_owner is owner
        process = creation_boundary.processes[-1]
        assert owner.process is process and not process.closed
        assert root.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["workspaces/local", "managed-workspaces", "federation/managed-workspaces"])
async def test_creation_observer_failure_keeps_published_runtime_and_original_exception(
    creation_app: FastAPI, creation_registry: GatewayWorkspaceRegistry, creation_boundary: CreationBoundary,
    tmp_path: Path, endpoint: str, caplog: pytest.LogCaptureFixture,
) -> None:
    registry = creation_registry
    root = tmp_path / "selected"
    root.mkdir()
    primary = RuntimeError(f"observer failed: {root}/private-observer")

    def observer():
        if any(registry.has_runtime(target.workspace_id) for target in registry.targets()):
            raise primary

    registry.add_commit_observer(observer)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=creation_app), base_url="http://gateway") as client:
            response = await client.post(f"/api/gateway/{endpoint}", json={"root_path": str(root)}, headers={"X-Request-ID": "req_creation_observer"})
        assert response.status_code == (500 if endpoint == "workspaces/local" else 502), response.text
        assert "private-observer" not in response.text and str(root) not in response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req_creation_observer"
        target, = registry.targets()
        owner = registry.backend_owner(target.workspace_id)
        assert owner is not None and owner.error is None and target.connection_error is None
        assert registry.managed_runtime(target.workspace_id).backend_owner is owner
        process = creation_boundary.processes[0]
        assert owner.process is process and not process.closed
        assert not owner.unpublished_runtimes
        assert any(record.exc_info and record.exc_info[1] is primary for record in caplog.records)
        cold = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=registry._storage_path, state_store=registry._state_store)
        assert cold.resolve(target.workspace_id).connection_error is None
        cold_owner = cold.backend_owner(target.workspace_id)
        assert cold_owner is not None and cold_owner.contract == owner.contract
    finally:
        registry.remove_commit_observer(observer)


@pytest.mark.asyncio
@pytest.mark.parametrize("sink_fails", [False, True])
async def test_creation_cancellation_preserves_identity_and_owner_cleanup(
    creation_app: FastAPI, creation_registry: GatewayWorkspaceRegistry, creation_boundary: CreationBoundary,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sink_fails: bool,
) -> None:
    primary = asyncio.CancelledError(f"cancelled: {tmp_path}/private-cancel")
    creation_boundary.error = primary
    if sink_fails:
        def fail_log(*_, **__):
            raise RuntimeError("diagnostic sink failed")
        monkeypatch.setattr("app.core.failures.logger.error", fail_log)
    root = tmp_path / "selected"
    root.mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=creation_app), base_url="http://gateway") as client:
        with pytest.raises(asyncio.CancelledError) as raised:
            await client.post("/api/gateway/managed-workspaces", json={"root_path": str(root)}, headers={"X-Request-ID": "req_creation_cancel"})
    assert raised.value is primary
    target, = creation_registry.targets()
    owner = creation_registry.backend_owner(target.workspace_id)
    assert owner is not None and owner.process is None and not owner.unpublished_runtimes
    assert owner.error is not None and owner.error == target.connection_error
    assert "error=CancelledError" in owner.error
    assert "private-cancel" not in owner.error
    assert creation_boundary.processes[0].closed
    assert creation_boundary.request_ids == ["req_creation_cancel"]
    assert ("diagnostic_sink_error=RuntimeError" in owner.error) is sink_fails


@pytest.mark.asyncio
async def test_creation_recording_failure_does_not_replace_original_start_error(
    creation_app: FastAPI, creation_registry: GatewayWorkspaceRegistry, creation_boundary: CreationBoundary,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    primary = RuntimeError(f"start failed: {tmp_path}/private-primary")
    secondary = OSError(5, "record failed", str(tmp_path / "private-record"))
    creation_boundary.error = primary

    def fail_record(*_):
        raise secondary

    monkeypatch.setattr(creation_registry, "mark_backend_error", fail_record)
    root = tmp_path / "selected"
    root.mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=creation_app), base_url="http://gateway") as client:
        response = await client.post("/api/gateway/managed-workspaces", json={"root_path": str(root)})
    assert response.status_code == 502 and "private-" not in response.text
    assert response.json()["detail"] == "工作区创建失败: stage=workspace_create, error=RuntimeError"
    assert any(record.exc_info and record.exc_info[1] is primary for record in caplog.records)
    assert any("记录Backend启动失败状态出错" in note and "private-record" in note for note in primary.__notes__)
    assert creation_boundary.processes[0].closed


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """R19 测试内 hermetic 修复：清掉本机代理环境变量。

    本文件用例不经外部网络；而环境的 NO_PROXY 含 IPv6 字面量（``::1``/
    ``[::1]``）会让用例内（或被测 gateway 代码内）构造的 httpx 客户端在
    代理解析时直接抛 ``InvalidURL: Invalid port: ':1]'``——属外部环境噪
    声混入测试。与 R18 对 gateway 会话上下文客户端的同类修复一致（任务
    书许可的 monkeypatch 清 ``*_proxy`` 做法）；清掉后客户端不取代理，
    与用例意图一致。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def controlled_controller_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def build(registry: GatewayWorkspaceRegistry) -> GatewayWorkspaceRuntimeController:
        controller = GatewayWorkspaceRuntimeController(
            registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs",
            drain_timeout_seconds=0,
        )
        async def action(*_, **__):
            return {}
        async def status(*_, **__):
            return {"lifecycle_state": "ready", "accepting_jobs": True, "blockers": []}
        monkeypatch.setattr(controller, "_runtime_action", action)
        monkeypatch.setattr(controller, "_runtime_status", status)
        return controller
    return build


@pytest.mark.asyncio
async def test_create_and_remove_direct_managed_workspace_preserves_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controlled_controller_factory,
) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "workspaces.json")
    workspace_root = tmp_path / "created" / "workspace"
    monkeypatch.setattr(
        "app.gateway.registry.runtime.resolve_backend_workspace_id",
        lambda *_, **__: "00000000-0000-4000-8000-000000000001",
    )

    backend = ProcessProbe("backend")
    async def start_runtime(*, owner, **_):
        owner.set_process(backend)
        member = owner.contract.members[0]
        contract = replace(owner.contract, backend_url="http://127.0.0.1:42000")
        runtime = WorkspaceRuntime(
            service_urls={"browser_manager": "http://127.0.0.1:42002"},
            backend_owner=owner, workspace_id=member.workspace_id,
        )
        owner.unpublished_runtimes[member.workspace_id] = runtime
        return contract, {member.workspace_id: runtime}
    async def listed():
        return []
    monkeypatch.setattr("app.gateway.runtime.group_actions.resolve_backend_workspace_id", lambda *_, **__: "00000000-0000-4000-8000-000000000001")
    async def bindings_valid(*_):
        return None
    monkeypatch.setattr("app.gateway.runtime.group_actions.validate_managed_backend_bindings", bindings_valid)
    monkeypatch.setattr("app.gateway.runtime.group_actions.start_managed_backend_runtime", start_runtime)
    monkeypatch.setattr(registry, "list_dtos", listed)

    target = await create_direct_managed_workspace(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
        root_path=str(workspace_root),
        name="Remote project",
        create_directory=True,
        request_id="req_create_direct",
    )

    assert workspace_root.is_dir()
    assert target.name == "Remote project"
    assert target.managed is True
    assert target.backend_url == "http://127.0.0.1:42000"
    assert registry.has_runtime(target.workspace_id) is True
    owner = registry.backend_owner(target.workspace_id)
    assert owner is not None and owner.process is backend
    assert owner.contract.workspace_ids == (target.workspace_id,)
    assert registry.resolve(target.workspace_id).root_path == str(workspace_root)

    with pytest.raises(ValueError, match="已经由当前 Gateway 托管") as duplicate:
        await create_direct_managed_workspace(
            registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs",
            root_path=str(workspace_root), name=None, create_directory=False,
            request_id="req_create_duplicate",
        )
    assert str(workspace_root) not in str(duplicate.value)

    registry.upsert(
        WorkspaceTarget(gateway_id=GATEWAY_ID,
            workspace_id='00000000-0000-4000-8000-000000000002',
            name="Kept",
            root_path=str(tmp_path / "kept"),
            backend_url="http://127.0.0.1:41235",
            connection_kind="local",

            managed=True,
        ),
        activate=False,
    )

    result = await remove_direct_managed_workspace(
        registry, target.workspace_id, controller=controlled_controller_factory(registry), request_id="remove",
    )
    assert result.status == "stopped"

    assert workspace_root.is_dir()
    assert backend.closed
    with pytest.raises(LookupError, match="不存在"):
        registry.resolve(target.workspace_id)


@pytest.mark.asyncio
async def test_create_direct_managed_workspace_requires_existing_directory_when_disabled(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "workspaces.json")

    with pytest.raises(FileNotFoundError, match="目录不存在"):
        await create_direct_managed_workspace(
            registry=registry,
            project_root=tmp_path,
            log_dir=tmp_path / "logs",
            root_path=str(tmp_path / "missing"),
            name=None,
            create_directory=False,
            request_id="req_create_missing",
        )

    with pytest.raises(ValueError, match="绝对路径"):
        await create_direct_managed_workspace(
            registry=registry,
            project_root=tmp_path,
            log_dir=tmp_path / "logs",
            root_path="relative/workspace",
            name=None,
            create_directory=True,
            request_id="req_create_relative",
        )


@pytest.mark.asyncio
async def test_remove_direct_managed_workspace_rejects_default_and_external_targets(
    tmp_path: Path, controlled_controller_factory,
) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(gateway_id=GATEWAY_ID,
            workspace_id='00000000-0000-4000-8000-000000000001',
            name="Default",
            root_path=str(tmp_path / "default"),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",

            managed=True,
            removable=False,
            system_default=True,
        )
    )
    registry.upsert(
        WorkspaceTarget(gateway_id=GATEWAY_ID,
            workspace_id='00000000-0000-4000-8000-000000000002',
            name="External",
            root_path=str(tmp_path / "external"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",

            managed=False,
        ),
        activate=False,
    )

    with pytest.raises(PermissionError, match="默认工作区"):
        await remove_direct_managed_workspace(registry, "00000000-0000-4000-8000-000000000001", controller=controlled_controller_factory(registry), request_id="default")
    with pytest.raises(ValueError, match="直接托管"):
        await remove_direct_managed_workspace(registry, "00000000-0000-4000-8000-000000000002", controller=controlled_controller_factory(registry), request_id="external")


@pytest.mark.asyncio
async def test_remove_direct_managed_workspace_keeps_last_target(tmp_path: Path, controlled_controller_factory) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=GATEWAY_ID, storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(gateway_id=GATEWAY_ID,
            workspace_id='00000000-0000-4000-8000-000000000001',
            name="Only",
            root_path=str(tmp_path / "only"),
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",

            managed=True,
        )
    )

    with pytest.raises(ValueError, match="至少需要保留"):
        await remove_direct_managed_workspace(registry, "00000000-0000-4000-8000-000000000001", controller=controlled_controller_factory(registry), request_id="only")


@pytest.mark.asyncio
async def test_remote_management_request_forwards_federation_and_request_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = FederationCredentialStore(
        storage_path=tmp_path / "federation.json"
    ).issue(
        connection_id="rgw_test",
        peer_gateway_id="gateway_remote",
    )

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"items": []}})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: client_class(
        **options, transport=httpx.MockTransport(respond), trust_env=False,
    ))

    result = await request_remote_gateway_management(
        gateway_url="http://127.0.0.1:41000/",
        credential=credential,
        method="POST",
        path="/api/gateway/federation/managed-workspaces",
        request_id="request-test",
        payload={"root_path": "/srv/project"},
    )

    assert result == {"items": []}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:41000/api/gateway/federation/managed-workspaces"
    assert request.headers["X-BoxTeam-Federation-Token"] == credential.token
    assert request.headers["X-Request-ID"] == "request-test"
    assert request.content == b'{"root_path":"/srv/project"}'
    assert request.extensions["timeout"] == {"connect": 45, "read": 45, "write": 45, "pool": 45}
