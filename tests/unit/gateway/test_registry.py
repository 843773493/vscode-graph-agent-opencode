import asyncio
import json
import re
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from app.core.mounted_workspaces import mounted_workspace_manifest_digest
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import (
    RemoteGatewayConnection,
    build_projected_workspace_id,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.registry import persistence as registry_persistence
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.workspace_ids import (
    build_workspace_id,
)
from app.services.infrastructure.config.state import ConfigConflictError
from tests.support.gateway_managed_backends import ProcessProbe


@pytest.fixture
def remote_registry_connection() -> RemoteGatewayConnection:
    return RemoteGatewayConnection(
        connection_id="rgw_persisted",
        name="持久化远程连接",
        host="remote.example",
        port=22,
        username="user",
        private_key_path=None,
        ssh_config_host="remote",
        remote_gateway_port=8014,
        remote_gateway_id="remote-gateway",
        protocol_version=1,
        source_owner="manual",
        remote_config_event_cursor=7,
        remote_config_state="restart_required",
        remote_restart_required=True,
        remote_candidate_ref="remote-candidate",
    )


@pytest.mark.parametrize("publication", ["config_batch", "snapshot", "upsert"])
def test_remote_gateway_sqlite_publication_survives_registry_reopen(
    tmp_path: Path,
    remote_registry_connection: RemoteGatewayConnection,
    publication: str,
) -> None:
    connection = remote_registry_connection
    if publication == "config_batch":
        connection = replace(connection, source_owner="config")
    target = WorkspaceTarget(
        workspace_id=build_projected_workspace_id(
            connection.connection_id, "remote-workspace"
        ),
        name="持久化远程工作区",
        root_path=None,
        backend_url="http://127.0.0.1:43100",
        connection_kind="remote_gateway",
        backend_workspace_id="00000000-0000-4000-8000-000000000002",
        owner="remote_projection",
        connection_id=connection.connection_id,
        remote_gateway_connection_id=connection.connection_id,
        remote_workspace_id="remote-workspace",
        remote_service_names=("workspace_api",),
    )
    state_path = tmp_path / "gateway.sqlite"
    registry_path = tmp_path / "gateway.json"
    with GatewayStateStore(path=state_path) as state:
        registry = GatewayWorkspaceRegistry(
            storage_path=registry_path, state_store=state
        )
        try:
            if publication == "config_batch":
                registry.apply_remote_projection_batch(
                    connections=(connection,),
                    runtimes={
                        connection.connection_id: WorkspaceRuntime(
                            service_urls={"workspace_api": target.backend_url}
                        )
                    },
                    projections={connection.connection_id: (target,)},
                )
            elif publication == "snapshot":
                registry.apply_remote_projection_snapshot(
                    connection=connection, projections=(target,), activate=True
                )
            else:
                registry.upsert_remote_gateway(connection)
                registry.upsert(target)
            revision = registry.registry_revision
            saved = state.load_workspace_registry()
            assert saved is not None
            assert saved["remote_gateway_connections"] == [asdict(connection)]
            assert (
                saved["targets"][0]["backend_workspace_id"]
                == target.backend_workspace_id
            )
        finally:
            registry.close()
    assert not registry_path.exists()
    with GatewayStateStore(path=state_path) as reopened_state:
        reopened = GatewayWorkspaceRegistry(
            storage_path=registry_path, state_store=reopened_state
        )
        try:
            assert reopened.registry_revision == revision
            assert (
                reopened.remote_gateway_connection(connection.connection_id)
                == connection
            )
            restored = reopened.resolve(target.workspace_id)
            assert restored.owner == "remote_projection"
            assert restored.remote_gateway_connection_id == connection.connection_id
            assert restored.backend_workspace_id == target.backend_workspace_id
            with pytest.raises(ConfigConflictError, match="游标回退"):
                reopened.apply_remote_projection_snapshot(
                    connection=replace(connection, remote_config_event_cursor=6),
                    projections=(restored,),
                )
            assert reopened_state.load_workspace_registry() == saved
        finally:
            reopened.close()


def test_removing_last_projection_deletes_connection_in_same_sqlite_publication(
    tmp_path: Path,
    remote_registry_connection: RemoteGatewayConnection,
) -> None:
    connection = remote_registry_connection
    state_path = tmp_path / "gateway.sqlite"
    registry_path = tmp_path / "gateway.json"
    with GatewayStateStore(path=state_path) as state:
        registry = GatewayWorkspaceRegistry(
            storage_path=registry_path, state_store=state
        )
        try:
            registry.upsert_remote_gateway(connection)
            for workspace_id in ("projection-a", "projection-b"):
                registry.upsert(
                    WorkspaceTarget(
                        workspace_id=workspace_id,
                        name=workspace_id,
                        root_path=None,
                        backend_url="http://127.0.0.1:43100",
                        connection_kind="remote_gateway",
                        backend_workspace_id="00000000-0000-4000-8000-000000000002",
                        owner="remote_projection",
                        connection_id=connection.connection_id,
                        remote_gateway_connection_id=connection.connection_id,
                        remote_workspace_id=workspace_id,
                    )
                )
            registry.remove("projection-a")
            assert (
                registry.remote_gateway_connection(connection.connection_id)
                == connection
            )
            registry.remove("projection-b")
            revision = registry.registry_revision
            saved = state.load_workspace_registry()
            assert saved is not None
            assert saved["remote_gateway_connections"] == []
            assert saved["targets"] == []
        finally:
            registry.close()
    with GatewayStateStore(path=state_path) as reopened_state:
        reopened = GatewayWorkspaceRegistry(
            storage_path=registry_path, state_store=reopened_state
        )
        try:
            assert reopened.registry_revision == revision
            assert reopened.remote_gateway_connections() == ()
            assert reopened.active_workspace_id is None
            assert reopened.targets() == ()
        finally:
            reopened.close()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("remote_config_event_cursor", True),
        ("remote_config_event_cursor", -1),
        ("remote_config_event_cursor", "7"),
        ("remote_config_state", 7),
        ("remote_restart_required", "false"),
        ("remote_candidate_ref", 7),
    ],
)
def test_registry_reopen_rejects_invalid_persisted_connection_fields(
    tmp_path: Path,
    remote_registry_connection: RemoteGatewayConnection,
    field: str,
    invalid_value: object,
) -> None:
    state_path = tmp_path / "gateway.sqlite"
    registry_path = tmp_path / "gateway.json"
    with GatewayStateStore(path=state_path) as state:
        registry = GatewayWorkspaceRegistry(
            storage_path=registry_path, state_store=state
        )
        try:
            registry.upsert_remote_gateway(remote_registry_connection)
            record = state.get_config("workspace_registry_meta")
            assert record is not None
            # 模拟绕过 registry owner 损坏持久记录；重开必须暴露非法值。
            record.payload["remote_gateway_connections"][0][field] = invalid_value
            state.set_config(
                config_key="workspace_registry_meta",
                config_version=record.config_version,
                payload=record.payload,
            )
        finally:
            registry.close()
    with GatewayStateStore(path=state_path) as reopened_state, pytest.raises(ValueError, match=field):
        GatewayWorkspaceRegistry(storage_path=registry_path, state_store=reopened_state)


class _HealthResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, str]:
        return {"workspace_id": "00000000-0000-4000-8000-000000000001"}


class _ConfigStatusResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, object]:
        return {
            "data": {
                "healthy": False,
                "revision": "revision-a",
                "restart_required": True,
                "reason": "restart_required",
                "changed_sections": ["mcp"],
                "last_error": "需要重启工作区后端",
            }
        }


class _ConfigAwareHealthClient:
    def __init__(self, *, timeout: int, trust_env: bool) -> None:
        assert timeout == 2
        assert trust_env is False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        assert headers == {
            "X-Local-Token": "local-dev-token",
            "X-BoxTeam-Workspace-Id": "00000000-0000-4000-8000-000000000001",
        }
        if url.endswith("/api/v1/config/reload-status"):
            return _ConfigStatusResponse()
        return _HealthResponse()


class _OrderedCloseProcess:
    pid = 100_002
    def __init__(self, name: str, events: list[str], process_count: int) -> None:
        self._name = name
        self._events = events
        self._process_count = process_count

    def request_terminate(self) -> None:
        self._events.append(f"terminate:{self._name}")

    def close(self, *, timeout_seconds: float = 8) -> None:
        assert timeout_seconds == 8
        assert (
            len([event for event in self._events if event.startswith("terminate:")])
            == self._process_count
        )
        self._events.append(f"close:{self._name}")

    def detach(self) -> None:
        self._events.append(f"detach:{self._name}")


class _ConcurrentHealthClient:
    active_requests = 0
    peak_requests = 0

    def __init__(self, *, timeout: int, trust_env: bool) -> None:
        assert timeout == 2
        assert trust_env is False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        assert url.endswith("/api/v1/health")
        assert headers == {
            "X-Local-Token": "local-dev-token",
            "X-BoxTeam-Workspace-Id": "00000000-0000-4000-8000-000000000001",
        }
        type(self).active_requests += 1
        type(self).peak_requests = max(
            type(self).peak_requests,
            type(self).active_requests,
        )
        await asyncio.sleep(0.02)
        type(self).active_requests -= 1
        return _HealthResponse()


def test_gateway_workspace_ids_use_32_hex_characters():
    local_id = build_workspace_id(
        "local",
        "/workspace/project",
        "http://127.0.0.1:8010",
    )
    remote_id = build_projected_workspace_id("rgw_test", "remote_workspace")

    assert re.fullmatch(r"gw_[0-9a-f]{32}", local_id)
    assert re.fullmatch(r"gw_[0-9a-f]{32}", remote_id)
    assert local_id != remote_id


def test_external_local_workspace_resolves_backend_without_gateway_runtime(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="external",
            name="External",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=False,
        )
    )

    assert registry.resolve_service_url("external", "workspace_api") == (
        "http://127.0.0.1:30100"
    )
    with pytest.raises(LookupError, match="工作区运行时尚未连接"):
        registry.resolve_service_url("external", "terminal_manager")


def test_registry_restricts_temporary_file_before_serializing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path = tmp_path / "gateway.json"
    restricted_paths: list[Path] = []
    restrict = registry_persistence.restrict_private_path_access

    def restrict_and_record(
        path: Path,
        *,
        is_directory: bool = False,
        descriptor: int | None = None,
    ) -> None:
        assert path.is_file()
        assert path.stat().st_size == 0
        assert descriptor is not None
        restrict(path, is_directory=is_directory, descriptor=descriptor)
        restricted_paths.append(path)

    monkeypatch.setattr(
        registry_persistence,
        "restrict_private_path_access",
        restrict_and_record,
    )
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    registry.upsert(
        WorkspaceTarget(
            workspace_id="external",
            name="External",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=False,
        )
    )

    assert len(restricted_paths) == 1
    assert not restricted_paths[0].exists()
    assert storage_path.is_file()
    assert '"workspace_id": "external"' in storage_path.read_text(encoding="utf-8")


def test_route_references_track_requests_and_streams_without_persisting_runtime_state(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "gateway.json"
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    registry.upsert(
        WorkspaceTarget(
            workspace_id="workspace",
            name="Workspace",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
        )
    )

    request_lease = registry.acquire_route_reference(
        "workspace",
        streaming=False,
    )
    stream_lease = registry.acquire_route_reference(
        "workspace",
        streaming=True,
    )
    assert request_lease.token == stream_lease.token
    assert registry.route_reference_counts("workspace") == (1, 1)

    registry.release_route_reference("workspace", streaming=False)
    assert registry.route_reference_counts("workspace") == (0, 1)
    registry.release_route_reference("workspace", streaming=True)
    assert registry.route_reference_counts("workspace") == (0, 0)
    with pytest.raises(RuntimeError, match="流引用重复释放"):
        registry.release_route_reference("workspace", streaming=True)

    restored = GatewayWorkspaceRegistry(storage_path=storage_path)
    assert restored.route_reference_counts("workspace") == (0, 0)


def test_registry_rejects_local_runtime_replacement_before_route_drain(tmp_path: Path, managed_backend_factory) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    target = WorkspaceTarget(
        workspace_id="workspace", name="Workspace", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        backend_workspace_id="00000000-0000-4000-8000-000000000001", managed=True,
    )
    registry.upsert(target)
    backend = ProcessProbe("old")
    graph = managed_backend_factory((target,), backend_url=target.backend_url, process=backend)
    graph.publish(registry)
    old_contract = graph.owner.contract
    old_runtime = graph.runtimes["workspace"]
    registry.acquire_route_reference("workspace", streaming=False)
    candidate = replace(old_contract, generation="backend_generation_candidate", backend_url="http://127.0.0.1:42000")
    with pytest.raises(RuntimeError, match="先完成排空"):
        registry.publish_backend_runtime(graph.owner, candidate, graph.runtimes)
    assert registry.managed_runtime("workspace") is old_runtime
    assert graph.owner.contract == old_contract and graph.owner.process is backend
    assert not backend.closed
    registry.release_route_reference("workspace", streaming=False)
    registry.close()


def test_remote_gateway_runtime_retires_until_proxy_references_are_drained(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    connection = RemoteGatewayConnection(
        connection_id="rgw_connection",
        name="Remote",
        host="remote.example",
        port=22,
        username="user",
        private_key_path=None,
        ssh_config_host="remote",
        remote_gateway_port=8014,
        remote_gateway_id="remote_gateway",
        protocol_version=1,
        source_owner="manual",
    )
    old_runtime = WorkspaceRuntime(
        service_urls={"workspace_api": "http://127.0.0.1:41000"},
        processes={"ssh_tunnel": _OrderedCloseProcess("old", events, 1)},
    )
    registry.upsert_remote_gateway(connection, runtime=old_runtime)
    registry.upsert(
        WorkspaceTarget(
            workspace_id="projected",
            name="Projected",
            root_path=None,
            backend_url="http://127.0.0.1:41000",
            connection_kind="remote_gateway",
            backend_workspace_id="00000000-0000-4000-8000-000000000002",
            owner="remote_projection",
            connection_id=connection.connection_id,
            remote_gateway_connection_id=connection.connection_id,
            remote_workspace_id="remote_workspace",
            remote_service_names=("workspace_api",),
        )
    )
    registry.acquire_route_reference("projected", streaming=True)

    new_runtime = WorkspaceRuntime(
        service_urls={"workspace_api": "http://127.0.0.1:42000"}
    )
    registry.upsert_remote_gateway(connection, runtime=new_runtime)
    assert registry.remote_gateway_url(connection.connection_id) == (
        "http://127.0.0.1:42000"
    )
    assert events == []

    registry.release_route_reference("projected", streaming=True)
    assert events == ["terminate:old", "close:old"]


def test_remote_projection_config_batch_is_atomic_and_respects_route_lease(
    tmp_path: Path,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "gateway.json",
        state_store=state,
    )
    connection = RemoteGatewayConnection(
        connection_id="rgw_batch",
        name="Remote batch",
        host="remote.example",
        port=22,
        username="user",
        private_key_path=None,
        ssh_config_host="remote",
        remote_gateway_port=8014,
        remote_gateway_id="remote-gateway",
        protocol_version=1,
        source_owner="config",
    )

    def target(url: str) -> WorkspaceTarget:
        return WorkspaceTarget(
            workspace_id="projected-batch",
            name="Projected batch",
            root_path=None,
            backend_url=url,
            connection_kind="remote_gateway",
            backend_workspace_id="00000000-0000-4000-8000-000000000002",
            owner="remote_projection",
            connection_id=connection.connection_id,
            remote_gateway_connection_id=connection.connection_id,
            remote_workspace_id="remote-workspace",
            remote_service_names=("workspace_api",),
        )

    try:
        registry.apply_remote_projection_batch(
            connections=(connection,),
            runtimes={
                connection.connection_id: WorkspaceRuntime(
                    service_urls={"workspace_api": "http://127.0.0.1:41000"}
                )
            },
            projections={connection.connection_id: (target("http://127.0.0.1:41000"),)},
        )
        original_generation = registry.resolve("projected-batch").target_generation
        registry.acquire_route_reference("projected-batch", streaming=True)

        with pytest.raises(RuntimeError, match="仍有引用的 route"):
            registry.apply_remote_projection_batch(
                connections=(connection,),
                runtimes={
                    connection.connection_id: WorkspaceRuntime(
                        service_urls={"workspace_api": "http://127.0.0.1:42000"}
                    )
                },
                projections={
                    connection.connection_id: (target("http://127.0.0.1:42000"),)
                },
            )

        assert registry.resolve("projected-batch").backend_url.endswith("41000")
        assert (
            registry.resolve("projected-batch").target_generation == original_generation
        )
        registry.release_route_reference("projected-batch", streaming=True)

        registry.apply_remote_projection_batch(
            connections=(connection,),
            runtimes={
                connection.connection_id: WorkspaceRuntime(
                    service_urls={"workspace_api": "http://127.0.0.1:42000"}
                )
            },
            projections={connection.connection_id: (target("http://127.0.0.1:42000"),)},
        )
        assert (
            registry.resolve("projected-batch").target_generation != original_generation
        )
        assert state.get_registry_revision() == 2
    finally:
        registry.close()
        state.close()


def test_remote_projection_batch_handoff_defers_old_runtime_and_can_rollback(
    tmp_path: Path,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "gateway.json",
        state_store=state,
    )

    class _TrackingRuntime(WorkspaceRuntime):
        def __init__(self, url: str) -> None:
            super().__init__(service_urls={"workspace_api": url})
            self.closed = False

        def close(self) -> None:
            self.closed = True

    connection = RemoteGatewayConnection(
        connection_id="rgw_handoff",
        name="Handoff remote",
        host="handoff.example",
        port=22,
        username="user",
        private_key_path=None,
        ssh_config_host="handoff",
        remote_gateway_port=8014,
        remote_gateway_id="handoff-gateway",
        protocol_version=1,
        source_owner="config",
    )

    def target(url: str) -> WorkspaceTarget:
        return WorkspaceTarget(
            workspace_id="handoff-workspace",
            name="Handoff workspace",
            root_path=None,
            backend_url=url,
            connection_kind="remote_gateway",
            backend_workspace_id="00000000-0000-4000-8000-000000000002",
            owner="remote_projection",
            connection_id=connection.connection_id,
            remote_gateway_connection_id=connection.connection_id,
            remote_workspace_id="handoff-workspace",
            remote_service_names=("workspace_api",),
        )

    old_runtime = _TrackingRuntime("http://127.0.0.1:43100")
    registry.apply_remote_projection_batch(
        connections=(connection,),
        runtimes={connection.connection_id: old_runtime},
        projections={
            connection.connection_id: (
                target(old_runtime.service_urls["workspace_api"]),
            )
        },
    )
    new_runtime = _TrackingRuntime("http://127.0.0.1:43200")
    handle = registry.apply_remote_projection_batch(
        connections=(connection,),
        runtimes={connection.connection_id: new_runtime},
        projections={
            connection.connection_id: (
                target(new_runtime.service_urls["workspace_api"]),
            )
        },
        defer_retiring_runtimes=True,
    )

    assert old_runtime.closed is False
    assert registry.remote_gateway_url(connection.connection_id) == (
        "http://127.0.0.1:43200"
    )
    handle.rollback()

    assert old_runtime.closed is False
    assert new_runtime.closed is True
    assert registry.remote_gateway_url(connection.connection_id) == (
        "http://127.0.0.1:43100"
    )
    assert registry.resolve("handoff-workspace").backend_url.endswith("43100")
    registry.close()
    state.close()


def test_remote_projection_batch_handoff_closes_old_runtime_only_on_promotion(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")

    class _TrackingRuntime(WorkspaceRuntime):
        def __init__(self, url: str) -> None:
            super().__init__(service_urls={"workspace_api": url})
            self.closed = False

        def close(self) -> None:
            self.closed = True

    connection = RemoteGatewayConnection(
        connection_id="rgw_promote",
        name="Promotion remote",
        host="promote.example",
        port=22,
        username="user",
        private_key_path=None,
        ssh_config_host="promote",
        remote_gateway_port=8014,
        remote_gateway_id="promote-gateway",
        protocol_version=1,
        source_owner="config",
    )
    old_runtime = _TrackingRuntime("http://127.0.0.1:43300")
    target = WorkspaceTarget(
        workspace_id="promote-workspace",
        name="Promotion workspace",
        root_path=None,
        backend_url="http://127.0.0.1:43300",
        connection_kind="remote_gateway",
        backend_workspace_id="00000000-0000-4000-8000-000000000002",
        owner="remote_projection",
        connection_id=connection.connection_id,
        remote_gateway_connection_id=connection.connection_id,
        remote_workspace_id="promote-workspace",
        remote_service_names=("workspace_api",),
    )
    registry.apply_remote_projection_batch(
        connections=(connection,),
        runtimes={connection.connection_id: old_runtime},
        projections={connection.connection_id: (target,)},
    )
    new_runtime = _TrackingRuntime("http://127.0.0.1:43400")
    target.backend_url = "http://127.0.0.1:43400"
    handle = registry.apply_remote_projection_batch(
        connections=(connection,),
        runtimes={connection.connection_id: new_runtime},
        projections={connection.connection_id: (target,)},
        defer_retiring_runtimes=True,
    )

    handle.promote()
    assert old_runtime.closed is True
    assert new_runtime.closed is False
    registry.close()


def test_registry_runtime_generation_protocol_is_persistent_and_reversible(tmp_path: Path, managed_backend_factory) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json", state_store=state)
    target = WorkspaceTarget(
        workspace_id="local-runtime", name="Local runtime", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        backend_workspace_id="00000000-0000-4000-8000-000000000001", managed=True,
    )
    registry.upsert(target)
    graph = managed_backend_factory((target,), backend_url=target.backend_url, process=ProcessProbe())
    graph.publish(registry)

    assert registry.prepare_runtime_generation("gateway-generation") is None
    assert registry.apply_runtime_generation("gateway-generation") is None
    workspace_previous = registry.apply_workspace_process_generation(
        "gateway-generation"
    )
    registry.promote_runtime_generation("gateway-generation")
    registry.promote_workspace_process_generation("gateway-generation")
    proof = registry.runtime_health_proof(
        consumer_id="workspace-process",
        generation="gateway-generation",
    )
    assert proof.details["managed_backend_count"] == 1
    assert proof.details["backends"][0]["affected_workspace_ids"] == ["local-runtime"]

    restored = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "gateway.json",
        state_store=state,
    )
    assert restored.runtime_generation == "gateway-generation"
    registry.rollback_workspace_process_generation(
        "gateway-generation",
        workspace_previous,
    )
    registry.rollback_runtime_generation("gateway-generation", None)
    assert registry.runtime_generation is None
    registry.close()
    restored.close()
    state.close()


def test_registry_persists_owner_namespace_and_generation(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    target = registry.upsert(
        WorkspaceTarget(
            workspace_id="configured",
            name="Configured",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            owner="config",
            target_namespace="gateway-config",
            connection_id="connection-1",
        )
    )

    restored = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    restored_target = restored.resolve("configured")
    assert restored_target.owner == "config"
    assert restored_target.target_namespace == "gateway-config"
    assert restored_target.connection_id == "connection-1"
    assert restored_target.target_generation == target.target_generation


def test_registry_cas_failure_restores_uncommitted_memory(tmp_path: Path) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        first = GatewayWorkspaceRegistry(
            storage_path=tmp_path / "gateway.json",
            state_store=state,
        )
        first.upsert(
            WorkspaceTarget(
                workspace_id="workspace",
                name="Original",
                root_path=str(tmp_path),
                backend_url="http://127.0.0.1:30100",
                connection_kind="local",
                backend_workspace_id="00000000-0000-4000-8000-000000000001",
            )
        )
        second = GatewayWorkspaceRegistry(
            storage_path=tmp_path / "gateway.json",
            state_store=state,
        )
        second.rename("workspace", "Committed")
        with pytest.raises(RuntimeError, match="CAS"):
            first.rename("workspace", "Uncommitted")
        assert first.resolve("workspace").name == "Original"
    finally:
        state.close()


def test_registry_signals_all_runtime_processes_before_waiting_for_exit(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    for index in range(2):
        process = _OrderedCloseProcess(f"workspace-{index}", events, 2)
        registry.upsert(
            WorkspaceTarget(
                workspace_id=f"workspace-{index}",
                name=f"Workspace {index}",
                root_path=str(tmp_path / f"workspace-{index}"),
                backend_url=f"http://127.0.0.1:{8100 + index}",
                connection_kind="local",
                backend_workspace_id="00000000-0000-4000-8000-000000000001",
            ),
            runtime=WorkspaceRuntime(
                service_urls={"workspace_api": f"http://127.0.0.1:{8100 + index}"},
                processes={"terminal_manager": process},
            ),
            activate=index == 0,
        )

    registry.close()

    assert events == [
        "terminate:workspace-0",
        "terminate:workspace-1",
        "close:workspace-0",
        "close:workspace-1",
    ]


@pytest.mark.parametrize("preserve_backend", [False, True])
def test_registry_can_detach_managed_services_for_gateway_restart(tmp_path: Path, managed_backend_factory, preserve_backend: bool) -> None:
    events: list[str] = []
    backend = ProcessProbe("backend", events)
    terminal = ProcessProbe("terminal", events)
    browser = ProcessProbe("browser", events)
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    target = WorkspaceTarget(
        workspace_id="workspace", name="Workspace", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:8100", connection_kind="local",
        backend_workspace_id="00000000-0000-4000-8000-000000000001", managed=True,
    )
    registry.upsert(target)
    graph = managed_backend_factory(
        (target,), backend_url=target.backend_url, process=backend,
        auxiliary_processes={"workspace": {"terminal_manager": terminal, "browser_manager": browser}},
    )
    graph.publish(registry)
    registry.close(preserve_browser_managers=True, preserve_terminal_managers=True, preserve_workspace_backends=preserve_backend)
    assert events == [f"{'detach' if preserve_backend else 'close'}:backend", "detach:browser", "detach:terminal"]
    assert graph.owner.process is None
    assert graph.runtimes["workspace"].processes == {}


@pytest.mark.parametrize("schema_version", [1, 5, 8, 11])
def test_registry_rejects_legacy_contract_without_rewriting_identity_or_running_intent(tmp_path: Path, schema_version: int) -> None:
    storage_path = tmp_path / "gateway.json"
    payload = {
        "schema_version": schema_version, "active_workspace_id": "old-active",
        "targets": [{"workspace_id": "old-active", "name": "Old", "root_path": "/workspace/managed",
                     "backend_url": "http://127.0.0.1:41000", "connection_kind": "local", "managed": True}],
    }
    original = json.dumps(payload)
    storage_path.write_text(original)
    with pytest.raises(ValueError, match="旧单工作区合同已失效"):
        GatewayWorkspaceRegistry(storage_path=storage_path)
    assert storage_path.read_text() == original


def test_registry_rejects_removed_ssh_direct_backend_in_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"schema_version": 13, "managed_backends": [], "targets": [{
        "workspace_id": "old-ssh", "name": "Old SSH", "root_path": "/workspace/remote",
        "backend_url": "http://127.0.0.1:41000", "connection_kind": "ssh", "managed": True,
    }]}))
    with pytest.raises(ValueError, match="SSH 直连后端注册记录"):
        GatewayWorkspaceRegistry(storage_path=path)


def test_registry_persists_auxiliary_manager_urls_for_gateway_restart(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "gateway.json"
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_browser_survival",
            name="Browser survival",
            root_path="/workspace/browser-survival",
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
            managed=True,
            local_service_urls={
                "terminal_manager": "http://127.0.0.1:41001",
                "browser_manager": "http://127.0.0.1:41002",
            },
        )
    )

    restored = GatewayWorkspaceRegistry(storage_path=storage_path)

    assert restored.resolve("gw_browser_survival").local_service_urls == {
        "terminal_manager": "http://127.0.0.1:41001",
        "browser_manager": "http://127.0.0.1:41002",
    }
    assert restored.resolve("gw_browser_survival").desired_running is False


@pytest.mark.asyncio
async def test_registry_persists_managed_runtime_intent_until_explicit_stop(
    tmp_path: Path, managed_backend_factory, monkeypatch,
) -> None:
    storage_path = tmp_path / "gateway.json"
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    target = WorkspaceTarget(
        workspace_id="gw_restore",
        name="Restore",
        root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000",
        connection_kind="local",
        backend_workspace_id="00000000-0000-4000-8000-000000000001",
        managed=True,
    )
    registry.upsert(target)
    graph = managed_backend_factory((target,), backend_url=target.backend_url, process=ProcessProbe())
    graph.publish(registry)

    assert registry.resolve(target.workspace_id).desired_running is True
    assert (
        GatewayWorkspaceRegistry(storage_path=storage_path)
        .resolve(target.workspace_id)
        .desired_running
        is True
    )

    controller = GatewayWorkspaceRuntimeController(
        registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs", drain_timeout_seconds=0,
    )
    async def action(*_, **__):
        return {}
    async def status(*_, **__):
        return {"lifecycle_state": "ready", "accepting_jobs": True, "blockers": []}
    async def listed():
        return []
    monkeypatch.setattr(controller, "_runtime_action", action)
    monkeypatch.setattr(controller, "_runtime_status", status)
    monkeypatch.setattr(registry, "list_dtos", listed)
    monkeypatch.setattr("app.gateway.runtime.local_workspace.resolve_backend_workspace_id", lambda *_, **__: target.backend_workspace_id)
    await controller.stop_managed_backend(target.workspace_id, request_id="stop")

    assert registry.resolve(target.workspace_id).desired_running is False
    assert (
        GatewayWorkspaceRegistry(storage_path=storage_path)
        .resolve(target.workspace_id)
        .desired_running
        is False
    )


@pytest.fixture
def registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    result = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    for index in range(3):
        result.upsert(
            WorkspaceTarget(
                workspace_id=f"workspace-{index}",
                name=f"Workspace {index}",
                root_path=str(tmp_path / f"workspace-{index}"),
                backend_url=f"http://127.0.0.1:{8100 + index}",
                connection_kind="local",
                backend_workspace_id="00000000-0000-4000-8000-000000000001",
            ),
            runtime=WorkspaceRuntime(
                service_urls={"workspace_api": f"http://127.0.0.1:{8100 + index}"}
            ),
            activate=index == 0,
        )
    return result


@pytest.mark.asyncio
async def test_list_dtos_checks_workspace_health_concurrently(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
):
    _ConcurrentHealthClient.active_requests = 0
    _ConcurrentHealthClient.peak_requests = 0
    monkeypatch.setattr(
        "app.gateway.registry.httpx.AsyncClient",
        _ConcurrentHealthClient,
    )

    result = await registry.list_dtos()

    assert [item.workspace_id for item in result] == [
        "workspace-0",
        "workspace-1",
        "workspace-2",
    ]
    assert [item.backend_workspace_id for item in result] == [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000001",
    ]
    assert all(item.status == "ready" for item in result)
    assert result[0].services["workspace_api"].local_port == 8100
    assert result[0].services["workspace_api"].health_path == "/api/v1/health"
    assert result[0].services["terminal_manager"].status == "unavailable"
    assert _ConcurrentHealthClient.peak_requests == 3


@pytest.mark.asyncio
async def test_list_dtos_can_skip_health_probes(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ConcurrentHealthClient.active_requests = 0
    _ConcurrentHealthClient.peak_requests = 0
    monkeypatch.setattr(
        "app.gateway.registry.httpx.AsyncClient",
        _ConcurrentHealthClient,
    )

    result = await registry.list_dtos(check_health=False)

    assert [item.workspace_id for item in result] == [
        "workspace-0",
        "workspace-1",
        "workspace-2",
    ]
    assert all(item.status == "ready" for item in result)
    assert _ConcurrentHealthClient.peak_requests == 0


@pytest.mark.asyncio
async def test_list_dtos_exposes_config_restart_requirement(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.gateway.registry.httpx.AsyncClient",
        _ConfigAwareHealthClient,
    )

    result = await registry.list_dtos()

    assert result[0].runtime_action == "probe_external_backend"
    assert result[0].config_reload.available is True
    assert result[0].config_reload.restart_required is True
    assert result[0].config_reload.reason == "restart_required"
    assert result[0].config_reload.changed_sections == ["mcp"]


def test_backend_publication_cas_restores_all_reused_runtime_and_owner_pointers(
    tmp_path: Path, managed_backend_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = GatewayStateStore(path=tmp_path / "backend-cas.sqlite")
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway-cas.json", state_store=state)
    targets = tuple(WorkspaceTarget(
        workspace_id=f"member-{index}", name=f"Member {index}", root_path=str(tmp_path / str(index)),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        backend_workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}", managed=True,
    ) for index in range(2))
    try:
        for target in targets:
            registry.upsert(target, activate=False)
        graph = managed_backend_factory(targets, backend_url=targets[0].backend_url, process=ProcessProbe())
        graph.publish(registry)
        old_contract = graph.owner.contract
        old_targets = {target.workspace_id: registry.resolve(target.workspace_id) for target in targets}
        old_urls = {member_id: runtime.service_urls for member_id, runtime in graph.runtimes.items()}
        other_writer = GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=state)
        other_writer.rename("member-0", "Committed elsewhere")
        replace_registry = state.replace_workspace_registry

        def commit_candidate(*args, **kwargs):
            assert graph.owner.contract is old_contract
            for member_id, runtime in graph.runtimes.items():
                assert registry.resolve(member_id) is old_targets[member_id]
                assert runtime.service_urls is old_urls[member_id]
                assert runtime.service_urls["workspace_api"] == old_contract.backend_url
            return replace_registry(*args, **kwargs)

        monkeypatch.setattr(state, "replace_workspace_registry", commit_candidate)
        candidate = replace(old_contract, generation="candidate-generation", backend_url="http://127.0.0.1:41010")
        with pytest.raises(RuntimeError, match="CAS"):
            registry.publish_backend_runtime(graph.owner, candidate, graph.runtimes)
        assert graph.owner.contract is old_contract
        assert registry.backend_owners() == (graph.owner,)
        for member_id, runtime in graph.runtimes.items():
            assert registry.managed_runtime(member_id) is runtime
            assert runtime.backend_owner is graph.owner
            assert runtime.service_urls == old_urls[member_id]
            assert registry.resolve(member_id) is old_targets[member_id]
            assert runtime.service_urls is old_urls[member_id]
        persisted = GatewayWorkspaceRegistry(storage_path=registry._storage_path, state_store=state)
        assert persisted.resolve("member-0").name == "Committed elsewhere"
        assert persisted.backend_owner("member-0").contract == old_contract
    finally:
        registry.close()
        state.close()


@pytest.fixture
def backend_dto_case(tmp_path: Path, managed_backend_factory):
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "dto-gateway.json")
    targets = tuple(WorkspaceTarget(
        workspace_id=f"dto-member-{index}", name=f"DTO {index}",
        root_path=str(tmp_path / str(index)), backend_url="http://127.0.0.1:41000",
        connection_kind="local", managed=True,
        backend_workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}",
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
async def test_backend_dtos_probe_published_contract_inside_lifecycle_lock(
    backend_dto_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, graph = backend_dto_case
    client_type = httpx.AsyncClient
    requests = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = {"data": {"healthy": True}}
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
            registry.resolve_service_url("dto-member-0", "workspace_api")
        items = await registry.list_dtos()
        assert all(item.status == "ready" and item.connection_error is None for item in items)
        assert all(item.backend_generation == graph.owner.contract.generation for item in items)
        assert all(item.affected_workspace_ids == list(graph.owner.contract.workspace_ids) for item in items)
        assert graph.owner.error is None
    assert registry.resolve_service_url("dto-member-0", "workspace_api") == graph.owner.contract.backend_url
    assert len([request for request in requests if request.url.path == "/api/v1/health"]) == 2
    assert all(registry.resolve(member_id).connection_error is None for member_id in graph.owner.contract.workspace_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_transition", [True, False])
async def test_backend_dto_probe_failure_is_visible_and_only_stable_owner_persists_it(
    backend_dto_case, monkeypatch: pytest.MonkeyPatch, during_transition: bool,
) -> None:
    registry, graph = backend_dto_case
    client_type = httpx.AsyncClient

    def response(request: httpx.Request) -> httpx.Response:
        payload = {"data": {"healthy": True}}
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
        assert all(item.status == "offline" and "健康证明不匹配" in item.connection_error for item in items)
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
    backend_dto_case, monkeypatch: pytest.MonkeyPatch,
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
    backend_dto_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, graph = backend_dto_case
    entry_active_workspace_id = registry.active_workspace_id
    activated_workspace_id = next(
        workspace_id
        for workspace_id in graph.owner.contract.workspace_ids
        if workspace_id != entry_active_workspace_id
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
    registry.activate(activated_workspace_id)
    release.set()

    items = await asyncio.wait_for(pending, 2)
    assert {
        item.workspace_id for item in items if item.active
    } == {entry_active_workspace_id}

    later_items = await asyncio.wait_for(registry.list_dtos(), 2)
    assert {
        item.workspace_id for item in later_items if item.active
    } == {activated_workspace_id}
