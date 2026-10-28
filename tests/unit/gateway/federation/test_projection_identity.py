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
from app.gateway.federation import RemoteGatewayConnection, build_projected_workspace_id
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.workspace import WorkspaceRuntime


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
            "workspace_id": "shared-remote-id", "name": f"Remote {index}",
            "backend_workspace_id": f"00000000-0000-4000-8000-{index:012d}",
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
            data = {"gateway_id": peer.gateway_id, "protocol_version": 1, "config_event_cursor": peer.cursor}
        else:
            assert request.url.path == "/api/gateway/federation/workspaces"
            data = {"gateway_id": peer.gateway_id, "protocol_version": 1, "items": peer.rows}
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
async def test_remote_identity_includes_connection_across_publish_refresh_and_cold_restore(
    tmp_path: Path, remote_peers: tuple[RemotePeer, ...],
    projection_state: GatewayStateStore, entry: str,
) -> None:
    path = tmp_path / "registry.json"
    state = projection_state
    ids = tuple(build_projected_workspace_id(peer.connection_id, "shared-remote-id") for peer in remote_peers)
    with closing(GatewayWorkspaceRegistry(storage_path=path, state_store=state)) as registry:
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
        assert {target.workspace_id for target in registry.targets()} == set(ids)
        assert registry.active_workspace_id == ids[0]
        for peer, workspace_id in zip(remote_peers, ids, strict=True):
            target = registry.resolve(workspace_id)
            assert target.owner == "remote_projection" and target.target_namespace == "gateway"
            assert target.remote_workspace_id == "shared-remote-id"
            assert target.connection_id == target.remote_gateway_connection_id == peer.connection_id
            assert target.backend_workspace_id == peer.rows[0]["backend_workspace_id"]
            assert registry.resolve_service_url(workspace_id, "workspace_api") == peer.url

        # 同一连接的相同远端身份仍须拒绝；不能通过取消去重来修复跨连接误判。
        before = state.load_workspace_registry()
        assert before is not None
        duplicate_payload = deepcopy(before)
        targets = duplicate_payload["targets"]
        assert isinstance(targets, list) and isinstance(targets[0], dict)
        duplicate = deepcopy(targets[0])
        duplicate["workspace_id"] = "duplicate-route-id"
        targets.append(duplicate)
        revision = registry.registry_revision
        with pytest.raises(ValueError, match="owner/namespace/identity 重复"):
            state.replace_workspace_registry(duplicate_payload, expected_revision=revision)
        assert state.load_workspace_registry() == before
        assert state.get_registry_revision() == revision
        assert state.list_registry_apply_journal()[-1]["state"] == "failed"

        untouched = asdict(registry.resolve(ids[1]))
        remote_peers[0].rows[0]["name"] = "Refreshed first peer"
        remote_peers[0].cursor = 2
        await remote_gateway.refresh_remote_gateway_projections(registry=registry, connection_id=remote_peers[0].connection_id)
        assert registry.resolve(ids[0]).name == "Refreshed first peer"
        assert registry.resolve(ids[0]).remote_config_event_cursor == 2
        assert asdict(registry.resolve(ids[1])) == untouched
        published = state.load_workspace_registry()
        expected = {workspace_id: asdict(registry.resolve(workspace_id)) for workspace_id in ids}

    state.close()
    with (
        GatewayStateStore(path=tmp_path / "gateway.sqlite") as cold_state,
        closing(GatewayWorkspaceRegistry(storage_path=path, state_store=cold_state)) as cold,
    ):
        assert cold_state.load_workspace_registry() == published
        assert {target.workspace_id: asdict(target) for target in cold.targets()} == expected
        assert cold.active_workspace_id == ids[0]
        for peer, workspace_id in zip(remote_peers, ids, strict=True):
            await remote_gateway.reconnect_remote_gateway(registry=cold, connection_id=peer.connection_id, log_dir=tmp_path / "logs")
            assert cold.resolve_service_url(workspace_id, "workspace_api") == peer.url
            assert cold.resolve(workspace_id).backend_workspace_id == peer.rows[0]["backend_workspace_id"]
        assert {target.workspace_id for target in cold.targets()} == set(ids)
