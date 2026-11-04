import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    RemoteGatewayConnection,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.registry import persistence as registry_persistence
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.services.infrastructure.config.state import ConfigConflictError
from tests.support.gateway_managed_backends import ProcessProbe

LOCAL_GATEWAY = "gateway_" + "0" * 32
REMOTE_GATEWAY = "gateway_" + "1" * 32
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
REMOTE_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


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
        remote_gateway_id=REMOTE_GATEWAY,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
        remote_config_event_cursor=7,
        remote_config_state="restart_required",
        remote_restart_required=True,
        remote_candidate_ref="remote-candidate",
    )


@pytest.mark.parametrize("publication", ["config_batch", "snapshot", "registered_snapshot"])
def test_remote_gateway_sqlite_publication_survives_registry_reopen(
    tmp_path: Path,
    remote_registry_connection: RemoteGatewayConnection,
    publication: str,
) -> None:
    connection = remote_registry_connection
    if publication == "config_batch":
        connection = replace(connection, source_owner="config")
    target = WorkspaceTarget(
        gateway_id=REMOTE_GATEWAY,
        workspace_id="00000000-0000-4000-8000-000000000002",
        name="持久化远程工作区",
        root_path=None,
        backend_url="http://127.0.0.1:43100",
        connection_kind="remote_gateway",
        owner="remote_projection",
        remote_gateway_connection_ids=(connection.connection_id,),
        remote_service_names=("workspace_api",),
    )
    state_path = tmp_path / "gateway.sqlite"
    registry_path = tmp_path / "gateway.json"
    with GatewayStateStore(path=state_path) as state:
        registry = GatewayWorkspaceRegistry(
            gateway_id=LOCAL_GATEWAY,
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
                registry.apply_remote_projection_snapshot(
                    connection=connection, projections=(target,), activate=True
                )
            revision = registry.registry_revision
            saved = state.load_workspace_registry()
            assert saved is not None
            assert saved["remote_gateway_connections"] == [asdict(connection)]
            saved_targets = saved["targets"]
            assert isinstance(saved_targets, list) and len(saved_targets) == 1
            assert saved_targets[0]["workspace_id"] == target.workspace_id
            assert saved_targets[0]["gateway_id"] == target.gateway_id
        finally:
            registry.close()
    assert not registry_path.exists()
    with GatewayStateStore(path=state_path) as reopened_state:
        reopened = GatewayWorkspaceRegistry(
            gateway_id=LOCAL_GATEWAY,
            storage_path=registry_path, state_store=reopened_state
        )
        try:
            assert reopened.registry_revision == revision
            assert (
                reopened.remote_gateway_connection(connection.connection_id)
                == connection
            )
            restored = reopened.resolve(target.key)
            assert restored.owner == "remote_projection"
            assert restored.remote_gateway_connection_ids == (connection.connection_id,)
            assert restored.key == target.key
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
            gateway_id=LOCAL_GATEWAY,
            storage_path=registry_path, state_store=state
        )
        try:
            registry.apply_remote_projection_snapshot(
                connection=connection,
                projections=tuple(
                    WorkspaceTarget(
                        gateway_id=REMOTE_GATEWAY,
                        workspace_id=workspace_id,
                        name=workspace_id,
                        root_path=None,
                        backend_url="http://127.0.0.1:43100",
                        connection_kind="remote_gateway",
                        owner="remote_projection",
                        remote_gateway_connection_ids=(connection.connection_id,),
                    )
                    for workspace_id in (WORKSPACE_ID, REMOTE_WORKSPACE_ID)
                ),
                activate=True,
            )
            registry.remove((REMOTE_GATEWAY, WORKSPACE_ID))
            assert (
                registry.remote_gateway_connection(connection.connection_id)
                == connection
            )
            registry.remove((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID))
            revision = registry.registry_revision
            saved = state.load_workspace_registry()
            assert saved is not None
            assert saved["remote_gateway_connections"] == []
            assert saved["targets"] == []
        finally:
            registry.close()
    with GatewayStateStore(path=state_path) as reopened_state:
        reopened = GatewayWorkspaceRegistry(
            gateway_id=LOCAL_GATEWAY,
            storage_path=registry_path, state_store=reopened_state
        )
        try:
            assert reopened.registry_revision == revision
            assert reopened.remote_gateway_connections() == ()
            assert reopened.active_workspace is None
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
            gateway_id=LOCAL_GATEWAY,
            storage_path=registry_path, state_store=state
        )
        try:
            registry.upsert_remote_gateway(remote_registry_connection)
            record = state.get_config("workspace_registry_meta")
            assert record is not None
            # 模拟绕过 registry owner 损坏持久记录；重开必须暴露非法值。
            connections = record.payload["remote_gateway_connections"]
            assert isinstance(connections, list) and len(connections) == 1
            connections[0][field] = invalid_value
            state.set_config(
                config_key="workspace_registry_meta",
                config_version=record.config_version,
                payload=record.payload,
            )
        finally:
            registry.close()
    with GatewayStateStore(path=state_path) as reopened_state, pytest.raises(ValueError, match=field):
        GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=registry_path, state_store=reopened_state)


class _HealthResponse:
    status_code = 200

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id

    def json(self) -> dict[str, str]:
        return {"workspace_id": self.workspace_id}


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
        port = urlparse(url).port
        assert port is not None
        workspace_id = f"00000000-0000-4000-8000-{port - 8099:012d}"
        assert headers == {
            "X-Local-Token": "local-dev-token",
            "X-BoxTeam-Workspace-Id": workspace_id,
        }
        if url.endswith("/api/v1/config/reload-status"):
            return _ConfigStatusResponse()
        return _HealthResponse(workspace_id)


class _OrderedCloseProcess:
    pid = 100_002
    def __init__(self, name: str, events: list[str], process_count: int) -> None:
        self._name = name
        self._events = events
        self._process_count = process_count
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def request_terminate(self) -> None:
        self._events.append(f"terminate:{self._name}")

    def close(self, *, timeout_seconds: float = 8) -> None:
        assert timeout_seconds == 8
        assert (
            len([event for event in self._events if event.startswith("terminate:")])
            == self._process_count
        )
        self._events.append(f"close:{self._name}")
        self._alive = False

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
        port = urlparse(url).port
        assert port is not None
        workspace_id = f"00000000-0000-4000-8000-{port - 8099:012d}"
        assert headers == {
            "X-Local-Token": "local-dev-token",
            "X-BoxTeam-Workspace-Id": workspace_id,
        }
        type(self).active_requests += 1
        type(self).peak_requests = max(
            type(self).peak_requests,
            type(self).active_requests,
        )
        await asyncio.sleep(0.02)
        type(self).active_requests -= 1
        return _HealthResponse(workspace_id)


def test_external_local_workspace_resolves_backend_without_gateway_runtime(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    registry.upsert(
        WorkspaceTarget(
            gateway_id=LOCAL_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000001",
            name="External",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
            managed=False,
        )
    )

    assert registry.resolve_service_url(WORKSPACE_ID, "workspace_api") == (
        "http://127.0.0.1:30100"
    )
    with pytest.raises(LookupError, match="工作区运行时尚未连接"):
        registry.resolve_service_url(WORKSPACE_ID, "terminal_manager")


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
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
    registry.upsert(
        WorkspaceTarget(
            gateway_id=LOCAL_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000001",
            name="External",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
            managed=False,
        )
    )

    assert len(restricted_paths) == 1
    assert not restricted_paths[0].exists()
    assert storage_path.is_file()
    assert json.loads(storage_path.read_text(encoding="utf-8"))["targets"][0]["workspace_id"] == WORKSPACE_ID


def test_route_references_track_requests_and_streams_without_persisting_runtime_state(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "gateway.json"
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
    registry.upsert(
        WorkspaceTarget(
            gateway_id=LOCAL_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000001",
            name="Workspace",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
        )
    )

    request_lease = registry.acquire_route_reference(
        WORKSPACE_ID,
        streaming=False,
    )
    stream_lease = registry.acquire_route_reference(
        WORKSPACE_ID,
        streaming=True,
    )
    assert request_lease.token == stream_lease.token
    assert registry.route_reference_counts(WORKSPACE_ID) == (1, 1)

    registry.release_route_reference(WORKSPACE_ID, streaming=False)
    assert registry.route_reference_counts(WORKSPACE_ID) == (0, 1)
    registry.release_route_reference(WORKSPACE_ID, streaming=True)
    assert registry.route_reference_counts(WORKSPACE_ID) == (0, 0)
    with pytest.raises(RuntimeError, match="流引用重复释放"):
        registry.release_route_reference(WORKSPACE_ID, streaming=True)

    restored = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
    assert restored.route_reference_counts(WORKSPACE_ID) == (0, 0)


def test_registry_rejects_local_runtime_replacement_before_route_drain(tmp_path: Path, managed_backend_factory) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    target = WorkspaceTarget(
        gateway_id=LOCAL_GATEWAY,
        workspace_id="00000000-0000-4000-8000-000000000001", name="Workspace", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        managed=True,
    )
    registry.upsert(target)
    backend = ProcessProbe("old")
    graph = managed_backend_factory((target,), backend_url=target.backend_url, process=backend)
    graph.publish(registry)
    old_contract = graph.owner.contract
    old_runtime = graph.runtimes[WORKSPACE_ID]
    registry.acquire_route_reference(WORKSPACE_ID, streaming=False)
    candidate = replace(old_contract, generation="backend_generation_candidate", backend_url="http://127.0.0.1:42000")
    with pytest.raises(RuntimeError, match="先完成排空"):
        registry.publish_backend_runtime(graph.owner, candidate, graph.runtimes)
    assert registry.managed_runtime(WORKSPACE_ID) is old_runtime
    assert graph.owner.contract == old_contract and graph.owner.process is backend
    assert not backend.closed
    registry.release_route_reference(WORKSPACE_ID, streaming=False)
    registry.close()


def test_remote_gateway_runtime_retires_until_proxy_references_are_drained(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    connection = RemoteGatewayConnection(
        connection_id="rgw_connection",
        name="Remote",
        host="remote.example",
        port=22,
        username="user",
        private_key_path=None,
        ssh_config_host="remote",
        remote_gateway_port=8014,
        remote_gateway_id=REMOTE_GATEWAY,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
    )
    old_runtime = WorkspaceRuntime(
        service_urls={"workspace_api": "http://127.0.0.1:41000"},
        processes={"ssh_tunnel": _OrderedCloseProcess("old", events, 1)},
    )
    registry.upsert_remote_gateway(connection, runtime=old_runtime)
    registry.upsert(
        WorkspaceTarget(
            gateway_id=REMOTE_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000002",
            name="Projected",
            root_path=None,
            backend_url="http://127.0.0.1:41000",
            connection_kind="remote_gateway",
            owner="remote_projection",
            remote_gateway_connection_ids=(connection.connection_id,),
            remote_service_names=("workspace_api",),
        )
    )
    registry.acquire_route_reference((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID), streaming=True)

    new_runtime = WorkspaceRuntime(
        service_urls={"workspace_api": "http://127.0.0.1:42000"}
    )
    registry.upsert_remote_gateway(connection, runtime=new_runtime)
    assert registry.remote_gateway_url(connection.connection_id) == (
        "http://127.0.0.1:42000"
    )
    assert events == []

    registry.release_route_reference((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID), streaming=True)
    assert events == ["terminate:old", "close:old"]


def test_config_batch_cannot_take_over_manual_connection_or_its_projection(
    tmp_path: Path, remote_registry_connection: RemoteGatewayConnection,
) -> None:
    connection = remote_registry_connection
    target = WorkspaceTarget(
        gateway_id=REMOTE_GATEWAY, workspace_id=REMOTE_WORKSPACE_ID, name="人工连接工作区",
        root_path=None, backend_url="http://127.0.0.1:43100", connection_kind="remote_gateway",
        owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,),
        remote_service_names=("workspace_api",),
    )
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as state:
        registry = GatewayWorkspaceRegistry(
            gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json", state_store=state,
        )
        try:
            runtime = WorkspaceRuntime(service_urls={"workspace_api": target.backend_url})
            registry.apply_remote_projection_snapshot(
                connection=connection, runtime=runtime, projections=(target,), activate=True,
            )
            before = state.load_workspace_registry()
            original_target = replace(registry.resolve(target.key))
            revision = registry.registry_revision
            with pytest.raises(ValueError, match="connection_id 与人工远程连接冲突"):
                registry.apply_remote_projection_batch(
                    connections=(replace(connection, source_owner="config"),),
                    runtimes={connection.connection_id: WorkspaceRuntime(service_urls={"workspace_api": "http://127.0.0.1:43200"})},
                    projections={connection.connection_id: (replace(target, backend_url="http://127.0.0.1:43200"),)},
                )
            assert state.load_workspace_registry() == before
            assert registry.registry_revision == revision
            assert registry.remote_gateway_connection(connection.connection_id) == connection
            assert registry.resolve(target.key) == original_target
            assert registry.active_workspace == target.key
            assert registry._remote_gateway_runtimes[connection.connection_id] is runtime
        finally:
            registry.close()
        reopened = GatewayWorkspaceRegistry(
            gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json", state_store=state,
        )
        try:
            assert reopened.remote_gateway_connection(connection.connection_id) == connection
            assert reopened.resolve(target.key) == original_target
            assert reopened.active_workspace == target.key
        finally:
            reopened.close()


def test_remote_projection_config_batch_is_atomic_and_respects_route_lease(
    tmp_path: Path,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        gateway_id=LOCAL_GATEWAY,
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
        remote_gateway_id=REMOTE_GATEWAY,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="config",
    )

    def target(url: str) -> WorkspaceTarget:
        return WorkspaceTarget(
            gateway_id=REMOTE_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000002",
            name="Projected batch",
            root_path=None,
            backend_url=url,
            connection_kind="remote_gateway",
            owner="remote_projection",
            remote_gateway_connection_ids=(connection.connection_id,),
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
        original_generation = registry.resolve((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID)).target_generation
        registry.acquire_route_reference((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID), streaming=True)

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

        assert registry.resolve((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID)).backend_url.endswith("41000")
        assert (
            registry.resolve((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID)).target_generation == original_generation
        )
        registry.release_route_reference((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID), streaming=True)

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
            registry.resolve((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID)).target_generation != original_generation
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
        gateway_id=LOCAL_GATEWAY,
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
        remote_gateway_id=REMOTE_GATEWAY,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="config",
    )

    def target(url: str) -> WorkspaceTarget:
        return WorkspaceTarget(
            gateway_id=REMOTE_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000002",
            name="Handoff workspace",
            root_path=None,
            backend_url=url,
            connection_kind="remote_gateway",
            owner="remote_projection",
            remote_gateway_connection_ids=(connection.connection_id,),
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
    assert registry.resolve((REMOTE_GATEWAY, REMOTE_WORKSPACE_ID)).backend_url.endswith("43100")
    registry.close()
    state.close()


def test_remote_projection_batch_handoff_closes_old_runtime_only_on_promotion(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")

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
        remote_gateway_id=REMOTE_GATEWAY,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="config",
    )
    old_runtime = _TrackingRuntime("http://127.0.0.1:43300")
    target = WorkspaceTarget(
        gateway_id=REMOTE_GATEWAY,
        workspace_id="00000000-0000-4000-8000-000000000002",
        name="Promotion workspace",
        root_path=None,
        backend_url="http://127.0.0.1:43300",
        connection_kind="remote_gateway",
        owner="remote_projection",
        remote_gateway_connection_ids=(connection.connection_id,),
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
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json", state_store=state)
    target = WorkspaceTarget(
        gateway_id=LOCAL_GATEWAY,
        workspace_id="00000000-0000-4000-8000-000000000001", name="Local runtime", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        managed=True,
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
    backends = proof.details["backends"]
    assert isinstance(backends, list) and len(backends) == 1
    assert backends[0]["affected_workspace_ids"] == [WORKSPACE_ID]

    restored = GatewayWorkspaceRegistry(
        gateway_id=LOCAL_GATEWAY,
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
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    target = registry.upsert(
        WorkspaceTarget(
            gateway_id=LOCAL_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000001",
            name="Configured",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:30100",
            connection_kind="local",
            owner="config",
            target_namespace="gateway-config",
            connection_id="connection-1",
        )
    )

    restored = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    restored_target = restored.resolve(WORKSPACE_ID)
    assert restored_target.owner == "config"
    assert restored_target.target_namespace == "gateway-config"
    assert restored_target.connection_id == "connection-1"
    assert restored_target.target_generation == target.target_generation


def test_registry_cas_failure_restores_uncommitted_memory(tmp_path: Path) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        first = GatewayWorkspaceRegistry(
            gateway_id=LOCAL_GATEWAY,
            storage_path=tmp_path / "gateway.json",
            state_store=state,
        )
        first.upsert(
            WorkspaceTarget(
                gateway_id=LOCAL_GATEWAY,
                workspace_id="00000000-0000-4000-8000-000000000001",
                name="Original",
                root_path=str(tmp_path),
                backend_url="http://127.0.0.1:30100",
                connection_kind="local",
            )
        )
        second = GatewayWorkspaceRegistry(
            gateway_id=LOCAL_GATEWAY,
            storage_path=tmp_path / "gateway.json",
            state_store=state,
        )
        second.rename(WORKSPACE_ID, "Committed")
        with pytest.raises(RuntimeError, match="CAS"):
            first.rename(WORKSPACE_ID, "Uncommitted")
        assert first.resolve(WORKSPACE_ID).name == "Original"
    finally:
        state.close()


def test_registry_signals_all_runtime_processes_before_waiting_for_exit(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    for index in range(2):
        process = _OrderedCloseProcess(f"workspace-{index}", events, 2)
        registry.upsert(
            WorkspaceTarget(
                gateway_id=LOCAL_GATEWAY,
                workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}",
                name=f"Workspace {index}",
                root_path=str(tmp_path / f"workspace-{index}"),
                backend_url=f"http://127.0.0.1:{8100 + index}",
                connection_kind="local",
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
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    target = WorkspaceTarget(
        gateway_id=LOCAL_GATEWAY,
        workspace_id="00000000-0000-4000-8000-000000000001", name="Workspace", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:8100", connection_kind="local",
        managed=True,
    )
    registry.upsert(target)
    graph = managed_backend_factory(
        (target,), backend_url=target.backend_url, process=backend,
        auxiliary_processes={WORKSPACE_ID: {"terminal_manager": terminal, "browser_manager": browser}},
    )
    graph.publish(registry)
    registry.close(preserve_browser_managers=True, preserve_terminal_managers=True, preserve_workspace_backends=preserve_backend)
    assert events == [f"{'detach' if preserve_backend else 'close'}:backend", "detach:browser", "detach:terminal"]
    assert graph.owner.process is None
    assert graph.runtimes[WORKSPACE_ID].processes == {}


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
    with pytest.raises(ValueError, match="身份合同需要显式迁移"):
        GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
    assert storage_path.read_text() == original


def test_registry_rejects_removed_ssh_direct_backend_in_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"schema_version": 14, "gateway_id": LOCAL_GATEWAY, "managed_backends": [], "targets": [{
        "gateway_id": LOCAL_GATEWAY, "workspace_id": WORKSPACE_ID, "name": "Old SSH", "root_path": None,
        "backend_url": "http://127.0.0.1:41000", "connection_kind": "ssh", "managed": True,
        "owner": "manual", "target_namespace": "gateway", "target_generation": "removed-ssh-generation",
        "remote_gateway_connection_ids": [],
    }]}))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="未知工作区连接类型: ssh"):
        GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=path)
    assert path.read_bytes() == original


def test_registry_persists_auxiliary_manager_urls_for_gateway_restart(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "gateway.json"
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
    registry.upsert(
        WorkspaceTarget(
            gateway_id=LOCAL_GATEWAY,
            workspace_id="00000000-0000-4000-8000-000000000001",
            name="Browser survival",
            root_path="/workspace/browser-survival",
            backend_url="http://127.0.0.1:41000",
            connection_kind="local",
            managed=True,
            local_service_urls={
                "terminal_manager": "http://127.0.0.1:41001",
                "browser_manager": "http://127.0.0.1:41002",
            },
        )
    )

    restored = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)

    assert restored.resolve(WORKSPACE_ID).local_service_urls == {
        "terminal_manager": "http://127.0.0.1:41001",
        "browser_manager": "http://127.0.0.1:41002",
    }
    assert restored.resolve(WORKSPACE_ID).desired_running is False


@pytest.mark.asyncio
async def test_registry_persists_managed_runtime_intent_until_explicit_stop(
    tmp_path: Path, managed_backend_factory, monkeypatch,
) -> None:
    storage_path = tmp_path / "gateway.json"
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
    target = WorkspaceTarget(
        gateway_id=LOCAL_GATEWAY,
        workspace_id="00000000-0000-4000-8000-000000000001",
        name="Restore",
        root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000",
        connection_kind="local",
        managed=True,
    )
    registry.upsert(target)
    graph = managed_backend_factory((target,), backend_url=target.backend_url, process=ProcessProbe())
    graph.publish(registry)

    assert registry.resolve(target.workspace_id).desired_running is True
    assert (
        GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
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
    monkeypatch.setattr("app.gateway.runtime.local_workspace.resolve_backend_workspace_id", lambda *_, **__: target.workspace_id)
    await controller.stop_managed_backend(target.workspace_id, request_id="stop")

    assert registry.resolve(target.workspace_id).desired_running is False
    assert (
        GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=storage_path)
        .resolve(target.workspace_id)
        .desired_running
        is False
    )


@pytest.fixture
def registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    result = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway.json")
    for index in range(3):
        result.upsert(
            WorkspaceTarget(
                gateway_id=LOCAL_GATEWAY,
                workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}",
                name=f"Workspace {index}",
                root_path=str(tmp_path / f"workspace-{index}"),
                backend_url=f"http://127.0.0.1:{8100 + index}",
                connection_kind="local",
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
        WORKSPACE_ID,
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000003",
    ]
    assert [(item.gateway_id, item.workspace_id) for item in result] == [
        (LOCAL_GATEWAY, f"00000000-0000-4000-8000-{index + 1:012d}") for index in range(3)
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
        WORKSPACE_ID,
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000003",
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
    registry = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=tmp_path / "gateway-cas.json", state_store=state)
    targets = tuple(WorkspaceTarget(
        gateway_id=LOCAL_GATEWAY,
        workspace_id=f"00000000-0000-4000-8000-{index + 1:012d}", name=f"Member {index}", root_path=str(tmp_path / str(index)),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
        managed=True,
    ) for index in range(2))
    try:
        for target in targets:
            registry.upsert(target, activate=False)
        graph = managed_backend_factory(targets, backend_url=targets[0].backend_url, process=ProcessProbe())
        graph.publish(registry)
        old_contract = graph.owner.contract
        old_targets = {target.workspace_id: registry.resolve(target.workspace_id) for target in targets}
        old_urls = {member_id: runtime.service_urls for member_id, runtime in graph.runtimes.items()}
        other_writer = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=registry._storage_path, state_store=state)
        other_writer.rename(WORKSPACE_ID, "Committed elsewhere")
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
        persisted = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY, storage_path=registry._storage_path, state_store=state)
        assert persisted.resolve(WORKSPACE_ID).name == "Committed elsewhere"
        persisted_owner = persisted.backend_owner(WORKSPACE_ID)
        assert persisted_owner is not None
        assert persisted_owner.contract == old_contract
    finally:
        registry.close()
        state.close()
