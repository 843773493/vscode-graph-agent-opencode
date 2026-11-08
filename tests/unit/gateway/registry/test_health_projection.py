"""Backend 列表健康投影、并发快照与原生错误边界。"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core import failures
from app.core.mounted_workspaces import mounted_workspace_manifest_digest
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import verify_gateway_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import workspaces
from tests.support.gateway_managed_backends import ManagedBackendGraph, ProcessProbe

LOCAL_GATEWAY_ID = "gateway_" + "1" * 32


@pytest.fixture
def health_registry_state(tmp_path: Path) -> Iterator[GatewayStateStore]:
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as state:
        yield state


@pytest.fixture
def backend_dto_case(
    tmp_path: Path, managed_backend_factory: Callable[..., ManagedBackendGraph],
    health_registry_state: GatewayStateStore,
) -> Iterator[tuple[GatewayWorkspaceRegistry, ManagedBackendGraph]]:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "dto-gateway.json", state_store=health_registry_state, gateway_id=LOCAL_GATEWAY_ID)
    targets = tuple(WorkspaceTarget(
        gateway_id=registry.gateway_id,
        workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}", name=f"DTO {index}",
        root_path=str(tmp_path / str(index)), backend_url="http://127.0.0.1:41000",
        connection_kind="local", managed=True,
    ) for index in range(2))
    for target in targets:
        registry.upsert(target, activate=False)
    graph = managed_backend_factory(targets, backend_url=targets[0].backend_url, process=ProcessProbe())
    graph.publish(registry)
    try:
        yield registry, graph
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_external_registered_route_is_discoverable_without_health_requests(
    tmp_path: Path, health_registry_state: GatewayStateStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "external-gateway.json", state_store=health_registry_state, gateway_id=LOCAL_GATEWAY_ID)
    client_type = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError(f"不检查健康的列表不应发请求: {request.url}")

    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", lambda **options: client_type(
        **options, transport=httpx.MockTransport(unexpected_request),
    ))
    try:
        external_id = "00000000-0000-4000-8000-000000000001"
        unconnected_id = "00000000-0000-4000-8000-000000000002"
        for workspace_id, name, backend in (
            (external_id, "external", "http://127.0.0.1:41000"),
            (unconnected_id, "unconnected", ""),
        ):
            registry.upsert(WorkspaceTarget(
                gateway_id=registry.gateway_id, workspace_id=workspace_id,
                name=name, root_path=str(tmp_path / name),
                connection_kind="local", managed=False, backend_url=backend,
            ), activate=False)
        before = health_registry_state.load_workspace_registry()
        targets = [asdict(target) for target in registry.targets()]
        revision, active = registry.registry_revision, registry.active_workspace
        leases = [registry.route_lease(target.key) for target in registry.targets()]
        items = {(item.gateway_id, item.workspace_id): item for item in await registry.list_dtos(check_health=False)}
        external = items[(registry.gateway_id, external_id)]
        assert external.status == external.services["workspace_api"].status == "ready"
        assert registry.resolve_service_url(external_id, "workspace_api") == "http://127.0.0.1:41000"
        assert items[(registry.gateway_id, unconnected_id)].status == "offline"
        assert not requests
        assert health_registry_state.load_workspace_registry() == before
        assert [asdict(target) for target in registry.targets()] == targets
        assert (registry.registry_revision, registry.active_workspace) == (revision, active)
        for lease in leases:
            assert not lease.invalidated.is_set()
            assert registry.route_lease((lease.gateway_id, lease.workspace_id)) == lease
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_backend_dtos_probe_published_contract_inside_lifecycle_lock(
    backend_dto_case: tuple[GatewayWorkspaceRegistry, ManagedBackendGraph], monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, graph = backend_dto_case
    client_type = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload: dict[str, object] = {"data": {"healthy": True}}
        if request.url.path == "/api/v1/health":
            payload = {
                "status": "ok",
                "workspace_id": request.headers["X-BoxTeam-Workspace-Id"],
                "backend_generation": graph.owner.contract.generation,
                "mount_manifest_digest": mounted_workspace_manifest_digest(registry.resolve_backend_mounts(graph.owner.contract)),
            }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", lambda **options: client_type(
        **options, transport=httpx.MockTransport(response),
    ))
    async with graph.owner.lock:
        with pytest.raises(LookupError, match="正在切换路由"):
            registry.resolve_service_url(graph.targets[0].key, "workspace_api")
        items = await registry.list_dtos()
        assert all(item.status == "ready" and item.connection_error is None for item in items)
        assert all(item.backend_generation == graph.owner.contract.generation for item in items)
        assert all(item.affected_workspace_ids == list(graph.owner.contract.workspace_ids) for item in items)
        assert graph.owner.error is None
    assert registry.resolve_service_url(graph.targets[0].key, "workspace_api") == graph.owner.contract.backend_url
    assert len([request for request in requests if request.url.path == "/api/v1/health"]) == 2
    assert all(registry.resolve(member_id).connection_error is None for member_id in graph.owner.contract.workspace_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_transition", [True, False])
async def test_backend_dto_probe_failure_is_visible_and_only_stable_owner_persists_it(
    backend_dto_case: tuple[GatewayWorkspaceRegistry, ManagedBackendGraph], monkeypatch: pytest.MonkeyPatch, during_transition: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry, graph = backend_dto_case
    client_type = httpx.AsyncClient

    def response(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = {"data": {"healthy": True}}
        if request.url.path == "/api/v1/health":
            payload = {
                "status": "ok", "workspace_id": request.headers["X-BoxTeam-Workspace-Id"],
                "backend_generation": "wrong-generation",
                "mount_manifest_digest": mounted_workspace_manifest_digest(registry.resolve_backend_mounts(graph.owner.contract)),
            }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", lambda **options: client_type(
        **options, transport=httpx.MockTransport(response),
    ))
    if during_transition:
        await graph.owner.lock.acquire()
    try:
        items = await registry.list_dtos()
        assert all(item.status == "offline" and item.connection_error is not None and "stage=workspace_health, error=RuntimeError" in item.connection_error for item in items)
        assert "managed Backend 完整组健康证明不匹配" in caplog.text
        if during_transition:
            assert graph.owner.error is None
            assert all(registry.resolve(member_id).connection_error is None for member_id in graph.owner.contract.workspace_ids)
        else:
            original_error = graph.owner.error
            assert original_error is not None
            await registry.list_dtos()
            assert graph.owner.error == original_error
    finally:
        if during_transition:
            graph.owner.lock.release()


@pytest.mark.asyncio
async def test_old_dto_probe_cannot_mark_newly_published_owner_failed(
    backend_dto_case: tuple[GatewayWorkspaceRegistry, ManagedBackendGraph], monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, graph = backend_dto_case
    old_contract = graph.owner.contract
    entered, release = asyncio.Event(), asyncio.Event()
    client_type = httpx.AsyncClient

    async def response(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/health":
            return httpx.Response(200, json={"data": {"healthy": True}})
        entered.set()
        await release.wait()
        return httpx.Response(200, json={
            "status": "ok", "workspace_id": request.headers["X-BoxTeam-Workspace-Id"],
            "backend_generation": "bad-old-proof",
            "mount_manifest_digest": mounted_workspace_manifest_digest(registry.resolve_backend_mounts(old_contract)),
        })

    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", lambda **options: client_type(
        **options, transport=httpx.MockTransport(response),
    ))
    pending = asyncio.create_task(registry.list_dtos())
    await asyncio.wait_for(entered.wait(), 2)
    candidate = replace(old_contract, generation="newly-published", backend_url="http://127.0.0.1:41020")
    registry.publish_backend_runtime(graph.owner, candidate, graph.runtimes)
    release.set()
    items = await asyncio.wait_for(pending, 2)
    assert all(item.status == "offline" and item.backend_generation == old_contract.generation for item in items)
    assert graph.owner.contract is candidate
    assert graph.owner.error is None
    assert all(registry.resolve(member_id).connection_error is None for member_id in candidate.workspace_ids)


@pytest.mark.asyncio
async def test_dto_active_state_uses_snapshot_from_list_entry_during_health_probe(
    backend_dto_case: tuple[GatewayWorkspaceRegistry, ManagedBackendGraph], monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, graph = backend_dto_case
    entry_active_workspace = registry.active_workspace
    assert entry_active_workspace is not None
    activated_workspace = next(
        target.key for target in graph.targets
        if target.key != entry_active_workspace
    )
    entered, release = asyncio.Event(), asyncio.Event()
    client_type = httpx.AsyncClient

    async def response(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/health":
            return httpx.Response(200, json={"data": {"healthy": True}})
        entered.set()
        await release.wait()
        return httpx.Response(200, json={
            "status": "ok",
            "workspace_id": request.headers["X-BoxTeam-Workspace-Id"],
            "backend_generation": graph.owner.contract.generation,
            "mount_manifest_digest": mounted_workspace_manifest_digest(registry.resolve_backend_mounts(graph.owner.contract)),
        })

    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", lambda **options: client_type(
        **options, transport=httpx.MockTransport(response),
    ))
    pending = asyncio.create_task(registry.list_dtos())
    await asyncio.wait_for(entered.wait(), 2)
    registry.activate(activated_workspace)
    release.set()

    items = await asyncio.wait_for(pending, 2)
    assert {
        (item.gateway_id, item.workspace_id) for item in items if item.active
    } == {entry_active_workspace}

    later_items = await asyncio.wait_for(registry.list_dtos(), 2)
    assert {
        (item.gateway_id, item.workspace_id) for item in later_items if item.active
    } == {activated_workspace}


@pytest.fixture(params=["/private/health-source", r"C:\private\health-source", r"\\server\private\health-source"])
def health_error(request: pytest.FixtureRequest) -> RuntimeError:
    return RuntimeError(f"native health rejected: {request.param}")


@pytest.fixture(params=[False, True], ids=["log", "failed-log"])
def health_sink_error(request: pytest.FixtureRequest, health_error: RuntimeError, monkeypatch: pytest.MonkeyPatch) -> OSError | None:
    if not request.param:
        return None
    error = OSError(5, "diagnostic sink rejected", str(health_error))

    def reject(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(failures.logger, "error", reject)
    return error


@pytest.fixture
def health_list_http() -> Callable[..., Awaitable[httpx.Response]]:
    original_client = httpx.AsyncClient

    async def listed(registry: GatewayWorkspaceRegistry, *, check_health: bool = True) -> httpx.Response:
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.include_router(workspaces.router)
        app.dependency_overrides[verify_gateway_token] = lambda: "test-token"
        app.dependency_overrides[workspaces.get_registry] = lambda: registry
        async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.get("/api/gateway/workspaces", params={"check_health": check_health}, headers={"X-Request-ID": "health-facts-01"})
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "health-facts-01"
        return response

    return listed


@pytest.mark.asyncio
@pytest.mark.parametrize(("fault", "stage"), [
    ("backend", "backend_runtime_health"), ("workspace", "workspace_health"),
    ("config", "config_status"),
    ("browser_manager", "service_health"),
])
async def test_health_failure_facts_cross_http_persistence_and_cold_read(
    backend_dto_case: tuple[GatewayWorkspaceRegistry, ManagedBackendGraph], health_registry_state: GatewayStateStore,
    health_error: RuntimeError, health_sink_error: OSError | None,
    health_list_http: Callable[..., Awaitable[httpx.Response]], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    fault: str, stage: str,
) -> None:
    registry, graph = backend_dto_case
    original_client = httpx.AsyncClient
    original_assert_healthy = graph.owner.assert_healthy
    fault_enabled = True
    before = health_registry_state.load_workspace_registry()
    bad_urls = {runtime.service_urls[fault] + "/health" for runtime in graph.runtimes.values()} if fault == "browser_manager" else set()

    def assert_healthy() -> None:
        if fault == "backend" and fault_enabled:
            raise health_error
        original_assert_healthy()

    def response(request: httpx.Request) -> httpx.Response:
        if fault_enabled and (
            fault == "workspace" and request.url.path == "/api/v1/health"
            or fault == "config" and request.url.path == "/api/v1/config/reload-status"
            or str(request.url) in bad_urls
        ):
            raise health_error
        payload: dict[str, object] = {"data": {"healthy": True}}
        if request.url.path == "/api/v1/health":
            payload = {
                "status": "ok", "workspace_id": request.headers["X-BoxTeam-Workspace-Id"],
                "backend_generation": graph.owner.contract.generation,
                "mount_manifest_digest": mounted_workspace_manifest_digest(registry.resolve_backend_mounts(graph.owner.contract)),
            }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(graph.owner, "assert_healthy", assert_healthy)
    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", lambda **options: original_client(**options, transport=httpx.MockTransport(response)))
    listed = await health_list_http(registry, check_health=fault != "backend")
    assert "health-source" not in listed.text
    items = listed.json()["data"]["items"]
    assert len(items) == 2
    for item in items:
        detail = item["config_reload"]["error"] if fault == "config" else item["services"][fault if fault == "browser_manager" else "workspace_api"]["error"]
        assert f"stage={stage}, error=RuntimeError" in detail
        assert ("diagnostic_sink_error=OSError" in detail) == (health_sink_error is not None)
        assert item["backend_generation"] == graph.owner.contract.generation
        assert item["affected_workspace_ids"] == list(graph.owner.contract.workspace_ids)
        if fault == "config":
            assert item["status"] == "ready" and item["connection_error"] is None
            assert not item["config_reload"]["available"]
        else:
            assert item["status"] == "offline" and item["connection_error"] == graph.owner.error
            assert registry.resolve(item["workspace_id"]).connection_error == item["connection_error"]
    if health_sink_error is None:
        assert any(record.exc_info and record.exc_info[1] is health_error for record in caplog.records)
    else:
        assert any(str(health_sink_error) in note for note in health_error.__notes__)
    if fault == "config":
        assert health_registry_state.load_workspace_registry() == before
    else:
        assert graph.owner.error and "health-source" not in graph.owner.error

    # 原探针故障消失不代表完整组已恢复，必须继续保留原错误直到生命周期owner发布恢复。
    fault_enabled = False
    if fault != "config":
        assert all(item["status"] == "offline" for item in (await health_list_http(registry, check_health=False)).json()["data"]["items"])
    else:
        repaired = await health_list_http(registry)
        assert all(item["config_reload"]["available"] and item["config_reload"]["error"] is None for item in repaired.json()["data"]["items"])
        assert health_registry_state.load_workspace_registry() == before
    persisted_failure = health_registry_state.load_workspace_registry()
    path = registry._storage_path
    registry.close()
    health_registry_state.close()
    with (
        GatewayStateStore(path=health_registry_state.path) as cold_state,
        closing(GatewayWorkspaceRegistry(storage_path=path, state_store=cold_state, gateway_id=registry.gateway_id)) as cold,
    ):
        assert cold_state.load_workspace_registry() == persisted_failure
        cold_response = await health_list_http(cold, check_health=False)
        assert "health-source" not in cold_response.text
        if fault != "config":
            # 冷registry尚无进程，列表报告当前未运行；持久原故障不能因此被改写。
            for item in cold_response.json()["data"]["items"]:
                assert item["status"] == "offline"
                assert "stage=backend_runtime_health, error=RuntimeError" in item["connection_error"]
                assert cold.resolve(item["workspace_id"]).connection_error == items[0]["connection_error"]
        assert cold_state.load_workspace_registry() == persisted_failure
