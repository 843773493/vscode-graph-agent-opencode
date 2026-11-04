"""统一 UUID 的 registry 权威键、连接关系和持久边界。"""

from dataclasses import replace
from pathlib import Path
from collections.abc import Iterator

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceIdentityConflictError, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.services.infrastructure.config.state import ConfigConflictError

LOCAL = "gateway_" + "1" * 32
REMOTE = "gateway_" + "2" * 32
WORKSPACE = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[GatewayWorkspaceRegistry]:
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as state:
        value = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", state_store=state, gateway_id=LOCAL)
        yield value
        value.close()


@pytest.fixture
def local_target(tmp_path: Path) -> WorkspaceTarget:
    return WorkspaceTarget(gateway_id=LOCAL, workspace_id=WORKSPACE, name="本机", root_path=str(tmp_path / "local"), backend_url="http://local.test", connection_kind="local")


@pytest.fixture
def connection() -> RemoteGatewayConnection:
    return RemoteGatewayConnection(connection_id="connection-one", name="远端", host="remote.test", port=22, username="user", private_key_path=None, ssh_config_host="remote", remote_gateway_port=8014, remote_gateway_id=REMOTE, protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual")


def projection(connection: RemoteGatewayConnection) -> WorkspaceTarget:
    return WorkspaceTarget(gateway_id=REMOTE, workspace_id=WORKSPACE, name="远端", root_path=None, backend_url="http://remote.test", connection_kind="remote_gateway", owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,), remote_service_names=("workspace_api",))


def publish(registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection) -> None:
    registry.apply_remote_projection_snapshot(connection=connection, projections=(projection(connection),), runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}))


def test_same_uuid_has_separate_owners_and_native_string_semantics(registry: GatewayWorkspaceRegistry, local_target: WorkspaceTarget, connection: RemoteGatewayConnection) -> None:
    registry.upsert(local_target)
    publish(registry, connection)
    assert len(registry.targets()) == 2
    assert registry.resolve(WORKSPACE) is local_target
    assert registry.resolve((REMOTE, WORKSPACE)).gateway_id == REMOTE
    registry.activate((REMOTE, WORKSPACE))
    assert registry.resolve().gateway_id == REMOTE
    assert registry.resolve(WORKSPACE).gateway_id == LOCAL
    with pytest.raises(RuntimeError, match="标准 UUID"):
        registry.resolve("gw_previous_alias")
    with pytest.raises(RuntimeError, match="标准 UUID"):
        registry.resolve("")


def test_root_conflict_is_checked_before_persistence(registry: GatewayWorkspaceRegistry, local_target: WorkspaceTarget, tmp_path: Path) -> None:
    registry.upsert(local_target)
    revision = registry.registry_revision
    with pytest.raises(WorkspaceIdentityConflictError):
        registry.upsert(replace(local_target, root_path=str(tmp_path / "another-root")))
    assert registry.registry_revision == revision
    assert registry.resolve(WORKSPACE).root_path == local_target.root_path


def test_leases_and_counts_are_partitioned_by_owner(registry: GatewayWorkspaceRegistry, local_target: WorkspaceTarget, connection: RemoteGatewayConnection) -> None:
    registry.upsert(local_target)
    publish(registry, connection)
    local_lease = registry.acquire_route_reference(local_target.key, streaming=False)
    remote_lease = registry.acquire_route_reference((REMOTE, WORKSPACE), streaming=True)
    assert local_lease.token != remote_lease.token
    assert registry.route_reference_counts(local_target.key) == (1, 0)
    assert registry.route_reference_counts((REMOTE, WORKSPACE)) == (0, 1)
    registry.invalidate_route(local_target.key)
    assert local_lease.invalidated.is_set()
    assert not remote_lease.invalidated.is_set()
    registry.release_route_reference(local_target.key, streaming=False)
    registry.release_route_reference((REMOTE, WORKSPACE), streaming=True)


def test_multi_connection_single_record_and_explicit_unbind(registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection) -> None:
    publish(registry, connection)
    second = replace(connection, connection_id="connection-two")
    publish(registry, second)
    assert len(registry.targets()) == 1
    target = registry.resolve((REMOTE, WORKSPACE))
    assert target.remote_gateway_connection_ids == ("connection-one", "connection-two")
    with pytest.raises(LookupError, match="不能唯一确定"):
        registry.resolve_service_url(target.key, "workspace_api")
    registry.remove_remote_gateway(connection.connection_id)
    retained = registry.resolve(target.key)
    assert retained.remote_gateway_connection_ids == (second.connection_id,)
    assert registry.resolve_service_url(retained.key, "workspace_api") == "http://remote.test"
    assert registry.remote_gateway_connection(second.connection_id) == second


def test_compound_active_parent_and_sqlite_reopen(registry: GatewayWorkspaceRegistry, local_target: WorkspaceTarget, connection: RemoteGatewayConnection, tmp_path: Path) -> None:
    registry.upsert(local_target)
    publish(registry, connection)
    registry.set_parent(local_target.key, (REMOTE, WORKSPACE))
    registry.activate((REMOTE, WORKSPACE))
    assert registry._state_store is not None
    payload = registry._state_store.load_workspace_registry()
    assert payload is not None
    assert payload["active_workspace"] == {"gateway_id": REMOTE, "workspace_id": WORKSPACE}
    assert "active_workspace_id" not in payload
    with registry._state_store.connection() as connection_db:
        assert [tuple(row) for row in connection_db.execute("SELECT gateway_id,workspace_id FROM gateway_workspace_registry ORDER BY gateway_id").fetchall()] == [(LOCAL, WORKSPACE), (REMOTE, WORKSPACE)]
    reopened = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", state_store=registry._state_store, gateway_id=LOCAL)
    assert reopened.active_workspace == (REMOTE, WORKSPACE)
    assert reopened.resolve(WORKSPACE).parent_workspace == (REMOTE, WORKSPACE)
    assert len(reopened.targets()) == 2
    assert all("backend_workspace_id" not in item and "remote_workspace_id" not in item for item in payload["targets"])


def test_failed_cas_keeps_previous_owner_graph(registry: GatewayWorkspaceRegistry, local_target: WorkspaceTarget, tmp_path: Path) -> None:
    registry.upsert(local_target)
    assert registry._state_store is not None
    stale = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", state_store=registry._state_store, gateway_id=LOCAL)
    registry.rename(WORKSPACE, "已更新")
    with pytest.raises(ConfigConflictError):
        stale.rename(WORKSPACE, "过期写入")
    assert stale.resolve(WORKSPACE).name == "本机"
    assert registry.resolve(WORKSPACE).name == "已更新"
