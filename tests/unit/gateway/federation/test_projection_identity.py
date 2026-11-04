"""从注册/配置入口验证远端投影身份；SSH 和 HTTP transport 使用明确替身。"""

from collections.abc import Iterator
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import pytest

from app.gateway import remote_gateway
from app.gateway.config import ConfiguredRemoteGateway
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.services.infrastructure.config.state import ConfigConflictError

LOCAL_GATEWAY = "gateway_" + "0" * 32
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@dataclass
class RemotePeer:
    host: str
    connection_id: str
    gateway_id: str
    rows: list[dict[str, object]]
    cursor: int = 1

    @property
    def url(self) -> str:
        return f"http://{self.host}:8014"


@pytest.fixture
def remote_peers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RemotePeer, ...]:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    peers = tuple(RemotePeer(
        host=f"remote-{index}.test", connection_id=f"rgw_{index}",
        gateway_id="gateway_" + str(index) * 32,
        rows=[{
            "workspace_id": WORKSPACE_ID, "name": f"Remote {index}",
            "managed": True, "connection_kind": "local", "services": ["workspace_api"],
        }],
    ) for index in (1, 2))
    store = FederationCredentialStore(storage_path=gateway_root / "credentials" / "federation.json")

    def pairing(*, connection_id: str, local_gateway_id: str, **_: object):
        return store.issue(connection_id=connection_id, peer_gateway_id=local_gateway_id)

    async def tunnel(*, connection: RemoteGatewayConnection, **_: object) -> WorkspaceRuntime:
        peer = next(peer for peer in peers if peer.connection_id == connection.connection_id)
        return WorkspaceRuntime(service_urls={"workspace_api": peer.url})

    def response(request: httpx.Request) -> httpx.Response:
        peer = next(peer for peer in peers if peer.host == request.url.host)
        assert request.headers["X-BoxTeam-Federation-Token"] == store.get(peer.connection_id).token
        assert request.headers["X-Request-ID"].startswith("req_")
        if request.url.path == "/api/gateway/federation/manifest":
            data = {"gateway_id": peer.gateway_id, "protocol_version": FEDERATION_PROTOCOL_VERSION, "config_event_cursor": peer.cursor}
        else:
            assert request.url.path == "/api/gateway/federation/workspaces"
            data = {"gateway_id": peer.gateway_id, "protocol_version": FEDERATION_PROTOCOL_VERSION, "items": peer.rows}
        return httpx.Response(200, json={"data": data})

    original_client = httpx.AsyncClient

    def client(*, timeout: float) -> httpx.AsyncClient:
        return original_client(timeout=timeout, transport=httpx.MockTransport(response))

    monkeypatch.setattr(remote_gateway, "obtain_pairing_credential_over_ssh", pairing)
    monkeypatch.setattr(remote_gateway, "start_remote_gateway_tunnel", tunnel)
    monkeypatch.setattr("app.gateway.federation.httpx.AsyncClient", client)
    return peers


@pytest.fixture
def projection_state(tmp_path: Path) -> Iterator[GatewayStateStore]:
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as state:
        yield state


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["manual", "config"])
async def test_remote_identity_preserves_owner_across_publish_refresh_and_cold_restore(
    tmp_path: Path, remote_peers: tuple[RemotePeer, ...],
    projection_state: GatewayStateStore, entry: str,
) -> None:
    path = tmp_path / "registry.json"
    state = projection_state
    keys = tuple((peer.gateway_id, WORKSPACE_ID) for peer in remote_peers)
    with closing(GatewayWorkspaceRegistry(storage_path=path, state_store=state, gateway_id=LOCAL_GATEWAY)) as registry:
        if entry == "manual":
            for peer in remote_peers:
                await remote_gateway.register_remote_gateway(
                    registry=registry, log_dir=tmp_path / "logs", name=peer.host,
                    host=peer.host, port=22, username="developer", private_key_path=None,
                    ssh_config_host=peer.host, remote_gateway_port=8014,
                    connection_id=peer.connection_id, activate=peer is remote_peers[0],
                )
        else:
            await remote_gateway.reconcile_configured_remote_gateways(
                registry=registry, log_dir=tmp_path / "logs",
                configured_workspaces=tuple(ConfiguredRemoteGateway(
                    host=peer.host, port=22, username="developer", private_key_path="",
                    ssh_config_host=peer.host, remote_gateway_port=8014,
                    connection_id=peer.connection_id, activate=peer is remote_peers[0],
                ) for peer in remote_peers),
                health_request_timeout_seconds=2, health_poll_interval_seconds=0.5,
            )
        assert len(registry.targets()) == 2
        assert {target.key for target in registry.targets()} == set(keys)
        assert {target.workspace_id for target in registry.targets()} == {WORKSPACE_ID}
        assert registry.active_workspace == keys[0]
        for peer, key in zip(remote_peers, keys, strict=True):
            target = registry.resolve(key)
            assert target.owner == "remote_projection"
            assert target.root_path is None
            assert target.connection_id is None
            assert target.remote_gateway_connection_ids == (peer.connection_id,)
            assert target.target_generation and target.runtime_lease_id
            assert registry.resolve_service_url(key, "workspace_api") == peer.url
            connection = registry.remote_gateway_connection(peer.connection_id)
            assert connection.remote_gateway_id == peer.gateway_id
            assert connection.protocol_version == FEDERATION_PROTOCOL_VERSION
            assert connection.source_owner == entry
            assert connection.remote_config_event_cursor == 1

        # 重复 owner 使用合法 UUID，必须抵达 SQLite 的身份去重边界。
        before = state.load_workspace_registry()
        assert before is not None
        assert before["active_workspace"] == {"gateway_id": keys[0][0], "workspace_id": WORKSPACE_ID}
        assert "active_workspace_id" not in before
        duplicate_payload = deepcopy(before)
        targets = duplicate_payload["targets"]
        assert isinstance(targets, list) and isinstance(targets[0], dict)
        assert all("backend_workspace_id" not in target and "remote_workspace_id" not in target for target in targets)
        targets.append(deepcopy(targets[0]))
        revision = registry.registry_revision
        with pytest.raises(ValueError, match="Gateway SQLite 工作区注册记录 workspace_id 重复"):
            state.replace_workspace_registry(duplicate_payload, expected_revision=revision)
        assert state.load_workspace_registry() == before
        assert state.get_registry_revision() == revision
        assert state.list_registry_apply_journal()[-1]["state"] == "failed"

        first_lease = registry.acquire_route_reference(keys[0], streaming=False)
        second_lease = registry.acquire_route_reference(keys[1], streaming=True)
        try:
            assert first_lease.token != second_lease.token
            assert registry.route_reference_counts(keys[0]) == (1, 0)
            assert registry.route_reference_counts(keys[1]) == (0, 1)
            initial_lease_ids = tuple(registry.resolve(key).runtime_lease_id for key in keys)
            assert initial_lease_ids[0] != initial_lease_ids[1]
            untouched = asdict(registry.resolve(keys[1]))
            untouched_connection = registry.remote_gateway_connection(remote_peers[1].connection_id)
            with closing(GatewayWorkspaceRegistry(storage_path=path, state_store=state, gateway_id=LOCAL_GATEWAY)) as stale:
                stale_targets = {target.key: asdict(target) for target in stale.targets()}
                stale_connections = stale.remote_gateway_connections()
                stale_revision = stale.registry_revision

                # 跳跃 cursor 来自真实发现入口的完整快照，元数据刷新保留两个 owner 的租约。
                remote_peers[0].rows[0]["name"] = "Refreshed first peer"
                remote_peers[0].cursor = 8
                await remote_gateway.refresh_remote_gateway_projections(registry=registry, connection_id=remote_peers[0].connection_id)
                assert registry.registry_revision == revision + 1
                assert registry.resolve(keys[0]).name == "Refreshed first peer"
                assert registry.remote_gateway_connection(remote_peers[0].connection_id).remote_config_event_cursor == 8
                assert asdict(registry.resolve(keys[1])) == untouched
                assert registry.remote_gateway_connection(remote_peers[1].connection_id) == untouched_connection
                refreshed = state.load_workspace_registry()
                refreshed_targets = {target.key: asdict(target) for target in registry.targets()}
                refreshed_revision = registry.registry_revision

                remote_peers[0].rows[0]["name"] = "Rejected older snapshot"
                remote_peers[0].cursor = 4
                with pytest.raises(ConfigConflictError, match="配置快照游标回退"):
                    await remote_gateway.refresh_remote_gateway_projections(registry=registry, connection_id=remote_peers[0].connection_id)
                assert state.load_workspace_registry() == refreshed
                assert registry.registry_revision == refreshed_revision
                assert {target.key: asdict(target) for target in registry.targets()} == refreshed_targets
                assert registry.remote_gateway_connection(remote_peers[0].connection_id).remote_config_event_cursor == 8

                # 新 cursor 也不能携带同一 owner 的重复行，更不能提前提交 cursor。
                remote_peers[0].cursor = 9
                remote_peers[0].rows.append(deepcopy(remote_peers[0].rows[0]))
                with pytest.raises(ValueError, match="远端连接快照包含重复工作区 owner"):
                    await remote_gateway.refresh_remote_gateway_projections(registry=registry, connection_id=remote_peers[0].connection_id)
                remote_peers[0].rows.pop()
                assert state.load_workspace_registry() == refreshed
                assert registry.registry_revision == refreshed_revision
                assert {target.key: asdict(target) for target in registry.targets()} == refreshed_targets
                assert registry.remote_gateway_connection(remote_peers[0].connection_id).remote_config_event_cursor == 8

                # 过期 writer 经真实重连入口准备新快照后，仍须在 CAS 失败时保留自身旧状态。
                journal_before_conflict = state.list_registry_apply_journal()
                with pytest.raises(ConfigConflictError, match="Gateway registry revision CAS 冲突"):
                    await remote_gateway.reconnect_remote_gateway(registry=stale, connection_id=remote_peers[0].connection_id, log_dir=tmp_path / "logs")
                assert stale.registry_revision == stale_revision
                assert {target.key: asdict(target) for target in stale.targets()} == stale_targets
                assert stale.remote_gateway_connections() == stale_connections
                with pytest.raises(LookupError, match="隧道尚未连接"):
                    stale.remote_gateway_url(remote_peers[0].connection_id)
                assert state.load_workspace_registry() == refreshed
                assert state.get_registry_revision() == refreshed_revision
                assert state.list_registry_apply_journal() == journal_before_conflict

            remote_peers[0].rows[0]["name"] = "Refreshed first peer"
            remote_peers[0].cursor = 8
            assert tuple(registry.resolve(key).runtime_lease_id for key in keys) == initial_lease_ids
            assert registry.route_lease(keys[0]).token == first_lease.token
            assert registry.route_lease(keys[1]).token == second_lease.token
            assert not first_lease.invalidated.is_set()
            assert not second_lease.invalidated.is_set()
            assert registry.route_reference_counts(keys[0]) == (1, 0)
            assert registry.route_reference_counts(keys[1]) == (0, 1)
            assert registry.active_workspace == keys[0]
            published = state.load_workspace_registry()
            expected = {target.key: asdict(target) for target in registry.targets()}
            expected_connections = {connection.connection_id: connection for connection in registry.remote_gateway_connections()}
        finally:
            registry.release_route_reference(keys[0], streaming=False)
            registry.release_route_reference(keys[1], streaming=True)

    state.close()
    with (
        GatewayStateStore(path=tmp_path / "gateway.sqlite") as cold_state,
        closing(GatewayWorkspaceRegistry(storage_path=path, state_store=cold_state, gateway_id=LOCAL_GATEWAY)) as cold,
    ):
        assert cold_state.load_workspace_registry() == published
        assert {target.key: asdict(target) for target in cold.targets()} == expected
        assert {connection.connection_id: connection for connection in cold.remote_gateway_connections()} == expected_connections
        assert cold.active_workspace == keys[0]
        for peer, key in zip(remote_peers, keys, strict=True):
            with pytest.raises(LookupError, match="隧道尚未连接"):
                cold.resolve_service_url(key, "workspace_api")
            await remote_gateway.reconnect_remote_gateway(registry=cold, connection_id=peer.connection_id, log_dir=tmp_path / "logs")
            assert cold.resolve_service_url(key, "workspace_api") == peer.url
            assert cold.remote_gateway_connection(peer.connection_id).remote_config_event_cursor == peer.cursor
        assert {target.key: asdict(target) for target in cold.targets()} == expected
        assert {connection.connection_id: connection for connection in cold.remote_gateway_connections()} == expected_connections
        assert cold.active_workspace == keys[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["request", "stream"])
async def test_same_source_connections_share_one_resource_until_explicit_unbind(
    tmp_path: Path, remote_peers: tuple[RemotePeer, ...],
    projection_state: GatewayStateStore, streaming: bool,
) -> None:
    remote_peers[1].gateway_id = remote_peers[0].gateway_id
    key = (remote_peers[0].gateway_id, WORKSPACE_ID)
    with closing(GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", state_store=projection_state, gateway_id=LOCAL_GATEWAY)) as registry:
        for peer in remote_peers:
            await remote_gateway.register_remote_gateway(
                registry=registry, log_dir=tmp_path / "logs", name=peer.host,
                host=peer.host, port=22, username="developer", private_key_path=None,
                ssh_config_host=peer.host, remote_gateway_port=8014,
                connection_id=peer.connection_id, activate=True,
            )
        assert len(registry.targets()) == 1
        target = registry.resolve(key)
        assert target.key == key
        assert target.remote_gateway_connection_ids == tuple(peer.connection_id for peer in remote_peers)
        assert registry.active_workspace == key
        ambiguous_lease = registry.route_lease(key)
        revision = registry.registry_revision
        published = projection_state.load_workspace_registry()
        with pytest.raises(LookupError, match="不能唯一确定"):
            registry.resolve_service_url(key, "workspace_api")
        with pytest.raises(LookupError, match="不能唯一确定"):
            registry.acquire_route_reference(key, streaming=streaming)
        assert registry.route_reference_counts(key) == (0, 0)
        assert registry.registry_revision == revision
        assert projection_state.load_workspace_registry() == published

        registry.remove_remote_gateway(remote_peers[0].connection_id)
        assert ambiguous_lease.invalidated.is_set()
        assert len(registry.targets()) == 1
        assert registry.resolve(key).remote_gateway_connection_ids == (remote_peers[1].connection_id,)
        assert registry.active_workspace == key
        assert registry.resolve_service_url(key, "workspace_api") == remote_peers[1].url
        assert tuple(connection.connection_id for connection in registry.remote_gateway_connections()) == (remote_peers[1].connection_id,)
        with pytest.raises(LookupError, match="未知远程 Gateway 连接"):
            registry.remote_gateway_connection(remote_peers[0].connection_id)
        lease = registry.acquire_route_reference(key, streaming=streaming)
        try:
            assert (lease.gateway_id, lease.workspace_id) == key
            assert registry.route_reference_counts(key) == ((0, 1) if streaming else (1, 0))
        finally:
            registry.release_route_reference(key, streaming=streaming)
        assert registry.route_reference_counts(key) == (0, 0)
