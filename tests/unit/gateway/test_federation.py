from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Self, cast

import httpx
import pytest
from starlette.requests import Request

import app.gateway.remote_gateway as remote_gateway_module
from app.gateway.auth import get_gateway_local_token
from app.gateway.config import ConfiguredRemoteGateway
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    PAIRING_SSH_TIMEOUT_SECONDS,
    RemoteGatewayConnection,
    discover_remote_gateway,
    obtain_pairing_credential_over_ssh,
    start_remote_gateway_tunnel,
)
from app.gateway.federation.errors import FederationError
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.registry.core import _REGISTRY_SCHEMA_VERSION
from app.gateway.remote_gateway import reconcile_configured_remote_gateways
from app.gateway.routes.workspaces_managed import _inbound_gateway_access_list
from app.gateway.runtime.consumer_protocol import GatewayRuntimeConsumerApplyError
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.workspace_proxy import _proxy_headers
from app.schemas.gateway.lifecycle import GatewayRuntimeGroupRequest
from app.services.infrastructure.config.state import ConfigConflictError

_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"
_OTHER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000003"
_REMOTE_GATEWAY_ID = "gateway_" + "a" * 32
_OTHER_GATEWAY_ID = "gateway_" + "b" * 32


def _response(data: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": data},
        request=httpx.Request("GET", "http://remote.test"),
    )


@dataclass(slots=True)
class _FederationClientState:
    manifest: dict[str, object]
    workspaces: dict[str, object]
    requests: list[tuple[str, dict[str, str]]]


@pytest.fixture
def federation_client_state(
    monkeypatch: pytest.MonkeyPatch,
) -> _FederationClientState:
    state = _FederationClientState(manifest={}, workspaces={}, requests=[])

    class FederationClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 10

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            state.requests.append((url, dict(headers)))
            assert headers["X-BoxTeam-Federation-Token"] == "federation-secret"
            if url.endswith("/manifest"):
                return _response(state.manifest)
            return _response(state.workspaces)

    monkeypatch.setattr("app.gateway.federation.httpx.AsyncClient", FederationClient)
    return state


def test_federation_credentials_are_separate_and_owner_only(tmp_path: Path) -> None:
    storage_path = tmp_path / "credentials" / "federation.json"
    store = FederationCredentialStore(storage_path=storage_path)

    issued = store.issue(
        connection_id="rgw_test",
        peer_gateway_id="gateway_peer",
        lifetime=timedelta(minutes=5),
    )

    assert store.get("rgw_test").token == issued.token
    assert store.verify(issued.token).peer_gateway_id == "gateway_peer"
    if os.name != "nt":
        assert storage_path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(storage_path.read_text(encoding="utf-8"))
    assert payload["credentials"][0]["token"] == issued.token


@pytest.mark.asyncio
async def test_inbound_access_excludes_credentials_held_for_outbound_gateways(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    local_gateway_id = load_or_create_gateway_id(gateway_root / "identity.json")
    store = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    )
    store.issue(
        connection_id="rgw_outbound",
        peer_gateway_id=local_gateway_id,
    )
    store.issue(
        connection_id=f"rgw_inbound:{local_gateway_id}",
        peer_gateway_id="gateway_external",
    )
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")

    async def list_dtos() -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                workspace_id=_WORKSPACE_ID,
                name="Local workspace",
                root_path=str(tmp_path / "local"),
                status="ready",
                managed=True,
                system_default=True,
                connection_kind="local",
            ),
            SimpleNamespace(
                workspace_id=_OTHER_WORKSPACE_ID,
                name="Remote projection",
                root_path="/remote/project",
                status="ready",
                managed=True,
                system_default=False,
                connection_kind="remote_gateway",
            ),
        ]

    monkeypatch.setattr(registry, "list_dtos", list_dtos)

    result = await _inbound_gateway_access_list(registry)

    assert result.gateway_id == local_gateway_id
    assert [peer.peer_gateway_id for peer in result.peers] == ["gateway_external"]
    assert [workspace.workspace_id for workspace in result.items] == [_WORKSPACE_ID]


def test_gateway_local_credential_is_generated_and_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path))

    first = get_gateway_local_token()
    second = get_gateway_local_token()

    assert first == second
    assert first != "local-dev-token"
    if os.name != "nt":
        assert (
            tmp_path / "credentials" / "local-token"
        ).stat().st_mode & 0o777 == 0o600


def test_pairing_does_not_read_global_docker_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(command: list[str], **_: object) -> SimpleNamespace:
        captured["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "peer_gateway_id": "remote_gateway",
                    "token": "federation-secret",
                    "expires_at": "2026-08-05T00:00:00+00:00",
                }
            ),
            stderr="",
        )

    monkeypatch.setenv(
        "BOXTEAM_REMOTE_PAIR_COMMAND",
        "docker-only-command --must-not-be-used",
    )
    monkeypatch.setattr("app.gateway.federation.subprocess.run", run)

    credential = obtain_pairing_credential_over_ssh(
        connection_id="rgw_test",
        local_gateway_id="gateway_local",
        host="remote.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="developer-server",
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[-1].startswith("boxteam gateway issue-federation-token")
    assert "docker-only-command" not in command[-1]
    assert credential.peer_gateway_id == "remote_gateway"


def test_pairing_rejects_empty_explicit_command() -> None:
    with pytest.raises(ValueError, match="配对命令不能为空"):
        obtain_pairing_credential_over_ssh(
            connection_id="rgw_test",
            local_gateway_id="gateway_local",
            host="remote.example.com",
            port=22,
            username="developer",
            private_key_path=None,
            ssh_config_host="developer-server",
            remote_pair_command="  ",
        )


def test_windows_ssh_config_uses_source_runtime_pairing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "peer_gateway_id": "remote_gateway",
                    "token": "token",
                    "expires_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            "",
        )

    monkeypatch.setattr("app.gateway.federation.subprocess.run", fake_run)

    obtain_pairing_credential_over_ssh(
        connection_id="rgw_test",
        local_gateway_id="gateway_local",
        host="127.0.0.1",
        port=22022,
        username="Administrator",
        private_key_path=None,
        ssh_config_host="a4500-windows-vm",
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "powershell -NoProfile" in command[-1]
    assert "boxteam-windows\\venv\\Scripts\\python.exe" in command[-1]
    assert "--connection-id rgw_test:gateway_local" in command[-1]


def test_registry_persists_remote_gateway_without_federation_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway"))
    storage_path = tmp_path / "gateway" / "workspaces.json"
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    connection = RemoteGatewayConnection(
        connection_id="rgw_test",
        name="Remote",
        host="remote.example.com",
        port=22,
        username="developer",
        private_key_path="/tmp/key",
        ssh_config_host=None,
        remote_gateway_port=8014,
        remote_gateway_id=_REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
    )
    registry.upsert_remote_gateway(
        connection,
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
    )
    registry.apply_remote_projection_snapshot(
        connection=registry.remote_gateway_connection("rgw_test"),
        projections=(WorkspaceTarget(
            gateway_id=_REMOTE_GATEWAY_ID,
            workspace_id=_WORKSPACE_ID,
            name="Remote workspace",
            root_path=None,
            backend_url="http://127.0.0.1:41000",
            connection_kind="remote_gateway",
            managed=True,
            remote_gateway_connection_ids=("rgw_test",),
            owner="remote_projection",
        ),),
    )

    serialized = storage_path.read_text(encoding="utf-8")
    assert "federation-secret" not in serialized
    restored = GatewayWorkspaceRegistry(storage_path=storage_path)
    target = restored.targets()[0]
    assert target.remote_gateway_connection_ids == ("rgw_test",)
    assert target.gateway_id == _REMOTE_GATEWAY_ID
    assert target.workspace_id == _WORKSPACE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_generation", [None, "gateway_generation_previous"])
async def test_configured_remote_reconcile_stages_all_and_preserves_manual_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_generation: str | None,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=state,
    )
    manual_connection = RemoteGatewayConnection(
        connection_id="rgw_manual",
        name="Manual",
        host="manual.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="manual",
        remote_gateway_port=8014,
        remote_gateway_id=_OTHER_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
    )
    registry.upsert_remote_gateway(manual_connection)
    registry.apply_remote_projection_snapshot(
        connection=registry.remote_gateway_connection("rgw_manual"),
        projections=(WorkspaceTarget(
            gateway_id=_OTHER_GATEWAY_ID,
            workspace_id=_WORKSPACE_ID,
            name="Manual projection",
            root_path=None,
            backend_url="http://manual",
            connection_kind="remote_gateway",
            owner="remote_projection",
            remote_gateway_connection_ids=("rgw_manual",),
        ),),
    )
    if runtime_generation is not None:
        registry.apply_runtime_generation(runtime_generation)
    initial_revision = registry.registry_revision

    async def prepare(
        *,
        configured: ConfiguredRemoteGateway,
        **_: object,
    ) -> tuple[RemoteGatewayConnection, WorkspaceRuntime, list[dict[str, object]]]:
        connection = RemoteGatewayConnection(
            connection_id=configured.connection_id,
            name=configured.name or configured.host,
            host=configured.host,
            port=configured.port,
            username=configured.username,
            private_key_path=None,
            ssh_config_host=configured.ssh_config_host,
            remote_gateway_port=configured.remote_gateway_port,
            remote_gateway_id=_REMOTE_GATEWAY_ID,
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            source_owner="config",
            remote_config_event_cursor=7,
            remote_config_state="pending_restart",
            remote_restart_required=True,
            remote_candidate_ref="remote-candidate-ref",
        )
        runtime = WorkspaceRuntime(
            service_urls={"workspace_api": f"http://{configured.connection_id}"}
        )
        return (
            connection,
            runtime,
            [
                {
                    'workspace_id': _WORKSPACE_ID,
                    'name': configured.name or configured.connection_id,
                    'managed': False,
                    'services': ['workspace_api'],
                }
            ],
        )

    monkeypatch.setattr(remote_gateway_module, "_prepare_remote_gateway", prepare)
    await reconcile_configured_remote_gateways(
        registry=registry,
        configured_workspaces=(
            ConfiguredRemoteGateway(
                connection_id="rgw_config",
                host="config.example.com",
                username="developer",
                private_key_path="/tmp/key",
            ),
        ),
        log_dir=tmp_path / "logs",
        health_request_timeout_seconds=2,
        health_poll_interval_seconds=0.01,
    )

    assert registry.registry_revision == initial_revision + 1
    assert registry.runtime_generation == runtime_generation
    assert registry.has_target((_OTHER_GATEWAY_ID, _WORKSPACE_ID))
    assert registry.has_target(
        (_REMOTE_GATEWAY_ID, _WORKSPACE_ID)
    )
    assert registry.remote_gateway_connection("rgw_manual").source_owner == "manual"
    assert registry.remote_gateway_connection("rgw_config").source_owner == "config"
    configured_connection = registry.remote_gateway_connection("rgw_config")
    assert configured_connection.remote_config_event_cursor == 7
    assert configured_connection.remote_config_state == "pending_restart"
    assert configured_connection.remote_restart_required is True
    assert configured_connection.remote_candidate_ref == "remote-candidate-ref"
    configured_target = registry.resolve((_REMOTE_GATEWAY_ID, _WORKSPACE_ID))
    assert registry.remote_connection_for_target(configured_target).remote_config_event_cursor == 7
    assert {target.key for target in registry.targets()} == {
        (_REMOTE_GATEWAY_ID, _WORKSPACE_ID), (_OTHER_GATEWAY_ID, _WORKSPACE_ID),
    }
    registry.close()
    state.close()


def test_remote_projection_keeps_one_owner_for_multiple_connections(
    tmp_path: Path,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", state_store=state)
    first = RemoteGatewayConnection(
        connection_id="rgw_first", name="First", host="first.example.com", port=22,
        username="developer", private_key_path=None, ssh_config_host=None,
        remote_gateway_port=8014, remote_gateway_id=_REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        remote_config_event_cursor=3,
    )
    second = replace(first, connection_id="rgw_second", host="second.example.com", remote_config_event_cursor=7)
    target = WorkspaceTarget(
        gateway_id=_REMOTE_GATEWAY_ID, workspace_id=_WORKSPACE_ID,
        name="Shared source", root_path=None, backend_url="http://first",
        connection_kind="remote_gateway", owner="remote_projection",
        remote_gateway_connection_ids=(first.connection_id,),
    )
    registry.apply_remote_projection_snapshot(connection=first, projections=(target,))
    registry.apply_remote_projection_snapshot(
        connection=second,
        projections=(replace(target, backend_url="http://second", remote_gateway_connection_ids=(second.connection_id,)),),
    )
    restored = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", state_store=state)
    [shared] = restored.targets()
    assert shared.key == target.key
    assert shared.remote_gateway_connection_ids == (first.connection_id, second.connection_id)
    assert restored.remote_gateway_connection(first.connection_id).remote_config_event_cursor == 3
    assert restored.remote_gateway_connection(second.connection_id).remote_config_event_cursor == 7
    with pytest.raises(LookupError, match="不能唯一确定"):
        restored.target_request_headers(shared)
    with pytest.raises(LookupError):
        restored.resolve(_WORKSPACE_ID)
    before_revision = restored.registry_revision
    restored.remove_remote_gateway(first.connection_id)
    assert restored.registry_revision == before_revision + 1
    [remaining] = restored.targets()
    assert remaining.key == shared.key
    assert remaining.remote_gateway_connection_ids == (second.connection_id,)
    assert restored.remote_connection_for_target(remaining) == second
    restored.close()
    registry.close()
    state.close()


def test_remote_projection_rejects_stale_cursor_and_accepts_full_snapshot_jump(
    tmp_path: Path,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=state,
    )

    def connection(cursor: int) -> RemoteGatewayConnection:
        return RemoteGatewayConnection(
            connection_id="rgw_cursor",
            name="Cursor remote",
            host="cursor.example.com",
            port=22,
            username="developer",
            private_key_path=None,
            ssh_config_host="cursor",
            remote_gateway_port=8014,
            remote_gateway_id=_REMOTE_GATEWAY_ID,
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            source_owner="config",
            remote_config_event_cursor=cursor,
        )

    def target() -> WorkspaceTarget:
        return WorkspaceTarget(
            gateway_id=_REMOTE_GATEWAY_ID,
            workspace_id=_WORKSPACE_ID,
            name="Cursor workspace",
            root_path=None,
            backend_url="http://cursor",
            connection_kind="remote_gateway",
            owner="remote_projection",
            remote_gateway_connection_ids=("rgw_cursor",),
        )

    registry.apply_remote_projection_snapshot(
        connection=connection(5),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://cursor"}),
        projections=(target(),),
    )
    initial_revision = registry.registry_revision

    with pytest.raises(ConfigConflictError, match="游标回退"):
        registry.apply_remote_projection_snapshot(
            connection=connection(4),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://stale"}),
            projections=(target(),),
        )

    assert registry.registry_revision == initial_revision
    assert (
        registry.remote_gateway_connection("rgw_cursor").remote_config_event_cursor == 5
    )
    assert registry.resolve(target().key).backend_url == "http://cursor"
    assert (
        registry.resolve(target().key).workspace_id
        == _WORKSPACE_ID
    )

    registry.apply_remote_projection_snapshot(
        connection=connection(9),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://resynced"}),
        projections=(target(),),
    )
    assert (
        registry.remote_gateway_connection("rgw_cursor").remote_config_event_cursor == 9
    )
    assert registry.remote_connection_for_target(registry.resolve(target().key)).remote_config_event_cursor == 9
    # UUID 变化表示替换工作区资源；原路由引用排空前不能移除旧 owner。
    workspace_key = target().key
    lease = registry.acquire_route_reference(workspace_key, streaming=True)
    replacement = replace(
        target(), workspace_id=_OTHER_WORKSPACE_ID
    )
    with pytest.raises(RuntimeError, match="仍有引用"):
        registry.apply_remote_projection_snapshot(
            connection=connection(10), projections=(replacement,)
        )
    assert (
        registry.resolve(workspace_key).workspace_id
        == _WORKSPACE_ID
    )
    assert registry.remote_gateway_connection("rgw_cursor").remote_config_event_cursor == 9
    registry.release_route_reference(workspace_key, streaming=True)
    registry.apply_remote_projection_snapshot(
        connection=connection(10), projections=(replacement,)
    )
    assert lease.invalidated.is_set()
    restored = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json", state_store=state
    )
    assert (
        restored.resolve(replacement.key).workspace_id
        == replacement.workspace_id
    )
    assert not restored.has_target(workspace_key)
    assert restored.remote_gateway_connection("rgw_cursor").remote_config_event_cursor == 10

    restored.close()
    registry.close()
    state.close()


@pytest.mark.asyncio
async def test_remote_gateway_registration_closes_staged_runtime_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TrackingRuntime(WorkspaceRuntime):
        def __init__(self, url: str) -> None:
            super().__init__(service_urls={"workspace_api": url})
            self.closed = False

        def close(self) -> None:
            self.closed = True

    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=state,
    )
    connection = RemoteGatewayConnection(
        connection_id="rgw_precommit",
        name="Precommit remote",
        host="precommit.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="precommit",
        remote_gateway_port=8014,
        remote_gateway_id=_REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
        remote_config_event_cursor=4,
    )
    old_runtime = _TrackingRuntime("http://old")
    old_target = WorkspaceTarget(
        gateway_id=connection.remote_gateway_id,
        workspace_id=_WORKSPACE_ID,
        name="Old workspace",
        root_path=None,
        backend_url="http://old",
        connection_kind="remote_gateway",
        owner="remote_projection",
        remote_gateway_connection_ids=(connection.connection_id,),
    )
    registry.apply_remote_projection_snapshot(
        connection=connection,
        projections=(old_target,),
        runtime=old_runtime,
    )
    previous_revision = registry.registry_revision
    previous_commit = registry._last_committed_snapshot
    previous_saved_state = state.load_workspace_registry()
    staged_runtime = _TrackingRuntime("http://staged")
    failure = OSError("registry save failed before commit")

    async def prepare_remote_gateway(**_: object):
        return (
            connection,
            staged_runtime,
            [
                {
                    'workspace_id': _OTHER_WORKSPACE_ID,
                    'name': 'New workspace',
                    'services': ['workspace_api'],
                }
            ],
        )

    def fail_before_save(**_: object) -> None:
        raise failure

    monkeypatch.setattr(
        remote_gateway_module, "_prepare_remote_gateway", prepare_remote_gateway
    )
    monkeypatch.setattr(registry, "_save", fail_before_save)

    with pytest.raises(OSError) as raised:
        await remote_gateway_module.register_remote_gateway(
            registry=registry,
            log_dir=tmp_path / "logs",
            name="Precommit remote",
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=None,
            ssh_config_host=connection.ssh_config_host,
            remote_gateway_port=connection.remote_gateway_port,
            connection_id=connection.connection_id,
        )

    assert raised.value is failure
    assert staged_runtime.closed is True
    assert registry.registry_revision == previous_revision
    assert registry._last_committed_snapshot is previous_commit
    assert state.load_workspace_registry() == previous_saved_state
    assert registry._remote_gateway_runtimes[connection.connection_id] is old_runtime
    assert registry.resolve(old_target.key).backend_url == "http://old"
    assert registry.remote_gateway_connection(connection.connection_id) == connection
    assert not registry.has_target(
        (connection.remote_gateway_id, _OTHER_WORKSPACE_ID)
    )
    registry.close()
    state.close()


@pytest.mark.asyncio
async def test_remote_gateway_registration_keeps_published_runtime_after_observer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TrackingRuntime(WorkspaceRuntime):
        def __init__(self, url: str) -> None:
            super().__init__(service_urls={"workspace_api": url})
            self.closed = False

        def close(self) -> None:
            self.closed = True

    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=state,
    )
    connection = RemoteGatewayConnection(
        connection_id="rgw_postcommit",
        name="Postcommit remote",
        host="postcommit.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="postcommit",
        remote_gateway_port=8014,
        remote_gateway_id=_REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
        remote_config_event_cursor=4,
    )
    old_runtime = _TrackingRuntime("http://old")
    old_target = WorkspaceTarget(
        gateway_id=connection.remote_gateway_id,
        workspace_id=_WORKSPACE_ID,
        name="Old workspace",
        root_path=None,
        backend_url="http://old",
        connection_kind="remote_gateway",
        owner="remote_projection",
        remote_gateway_connection_ids=(connection.connection_id,),
    )
    registry.apply_remote_projection_snapshot(
        connection=connection,
        projections=(old_target,),
        runtime=old_runtime,
    )
    previous_revision = registry.registry_revision
    previous_commit = registry._last_committed_snapshot
    staged_runtime = _TrackingRuntime("http://published")
    failure = RuntimeError("registry observer failed after commit")

    async def prepare_remote_gateway(**_: object):
        return (
            replace(connection, remote_config_event_cursor=5),
            staged_runtime,
            [
                {
                    'workspace_id': _OTHER_WORKSPACE_ID,
                    'name': 'New workspace',
                    'services': ['workspace_api'],
                }
            ],
        )

    def fail_after_commit() -> None:
        raise failure

    monkeypatch.setattr(
        remote_gateway_module, "_prepare_remote_gateway", prepare_remote_gateway
    )
    registry.add_commit_observer(fail_after_commit)
    try:
        with pytest.raises(RuntimeError) as raised:
            await remote_gateway_module.register_remote_gateway(
                registry=registry,
                log_dir=tmp_path / "logs",
                name="Postcommit remote",
                host=connection.host,
                port=connection.port,
                username=connection.username,
                private_key_path=None,
                ssh_config_host=connection.ssh_config_host,
                remote_gateway_port=connection.remote_gateway_port,
                connection_id=connection.connection_id,
            )
        assert raised.value is failure
        assert registry.registry_revision == previous_revision + 1
        assert registry._last_committed_snapshot is not previous_commit
        assert state.get_registry_revision() == registry.registry_revision
        new_workspace_id = _OTHER_WORKSPACE_ID
        assert registry.resolve((connection.remote_gateway_id, new_workspace_id)).backend_url == "http://published"
        assert registry.remote_gateway_url(connection.connection_id) == "http://published"
        assert registry._remote_gateway_runtimes[connection.connection_id] is staged_runtime
        assert staged_runtime.closed is False
        assert old_runtime.closed is True
        assert (
            registry.remote_gateway_connection(connection.connection_id)
            .remote_config_event_cursor
            == 5
        )
        saved = state.load_workspace_registry()
        assert saved is not None
        assert any(
            target["workspace_id"] == new_workspace_id
            and target["backend_url"] == "http://published"
            for target in saved["targets"]
        )
    finally:
        registry.remove_commit_observer(fail_after_commit)
        registry.close()
        state.close()


@pytest.mark.asyncio
async def test_configured_remote_reconcile_keeps_published_runtime_after_observer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TrackingRuntime(WorkspaceRuntime):
        def __init__(self, url: str) -> None:
            super().__init__(service_urls={"workspace_api": url})
            self.closed = False

        def close(self) -> None:
            self.closed = True

    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=state,
    )
    previous_revision = registry.registry_revision
    previous_commit = registry._last_committed_snapshot
    configured = ConfiguredRemoteGateway(
        connection_id="rgw_batch_observer",
        host="batch.example.com",
        username="developer",
        private_key_path="/tmp/key",
    )
    runtime = _TrackingRuntime("http://batch-published")

    async def prepare(
        *, configured: ConfiguredRemoteGateway, **_: object
    ) -> tuple[RemoteGatewayConnection, WorkspaceRuntime, list[dict[str, object]]]:
        connection = RemoteGatewayConnection(
            connection_id=configured.connection_id,
            name=configured.name or configured.host,
            host=configured.host,
            port=configured.port,
            username=configured.username,
            private_key_path=None,
            ssh_config_host=configured.ssh_config_host,
            remote_gateway_port=configured.remote_gateway_port,
            remote_gateway_id=_REMOTE_GATEWAY_ID,
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            source_owner="config",
            remote_config_event_cursor=1,
        )
        return (
            connection,
            runtime,
            [
                {
                    'workspace_id': _WORKSPACE_ID,
                    'name': 'Batch workspace',
                    'managed': False,
                    'services': ['workspace_api'],
                }
            ],
        )

    failure = RuntimeError("batch registry observer failed after commit")

    def fail_after_commit() -> None:
        raise failure

    monkeypatch.setattr(remote_gateway_module, "_prepare_remote_gateway", prepare)
    registry.add_commit_observer(fail_after_commit)
    try:
        with pytest.raises(GatewayRuntimeConsumerApplyError) as raised:
            await reconcile_configured_remote_gateways(
                registry=registry,
                configured_workspaces=(configured,),
                log_dir=tmp_path / "logs",
                health_request_timeout_seconds=2,
                health_poll_interval_seconds=0.01,
            )
        workspace_id = _WORKSPACE_ID
        assert raised.value.original_error is failure
        assert raised.value.__cause__ is failure
        assert raised.value.recovery_required is False
        assert raised.value.consumer_states == [
            {"consumer_id": "remote-projection", "details": {}},
            {"consumer_id": "registry-batch", "details": {}},
        ]
        assert registry.registry_revision == previous_revision + 1
        assert registry._last_committed_snapshot is not previous_commit
        assert state.get_registry_revision() == registry.registry_revision
        assert registry.resolve((_REMOTE_GATEWAY_ID, workspace_id)).backend_url == "http://batch-published"
        assert registry._remote_gateway_runtimes[configured.connection_id] is runtime
        assert registry.remote_gateway_url(configured.connection_id) == "http://batch-published"
        assert runtime.closed is False
        saved = state.load_workspace_registry()
        assert saved is not None
        assert any(
            target["workspace_id"] == workspace_id
            and target["backend_url"] == "http://batch-published"
            for target in saved["targets"]
        )
    finally:
        registry.remove_commit_observer(fail_after_commit)
        registry.close()
        state.close()


@pytest.mark.asyncio
async def test_configured_remote_reconcile_keeps_old_projection_when_peer_is_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json",
        state_store=state,
    )
    old_connection = RemoteGatewayConnection(
        connection_id="rgw_configured",
        name="Old remote",
        host="old.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="old",
        remote_gateway_port=8014,
        remote_gateway_id=_OTHER_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="config",
    )
    registry.apply_remote_projection_batch(
        connections=(old_connection,),
        runtimes={
            old_connection.connection_id: WorkspaceRuntime(
                service_urls={"workspace_api": "http://old"}
            )
        },
        projections={
            old_connection.connection_id: (
                WorkspaceTarget(
                    gateway_id=old_connection.remote_gateway_id,
                    workspace_id=_WORKSPACE_ID,
                    name="Old workspace",
                    root_path=None,
                    backend_url="http://old",
                    connection_kind="remote_gateway",
                    owner="remote_projection",
                    remote_gateway_connection_ids=(old_connection.connection_id,),
                ),
            )
        },
    )
    initial_revision = registry.registry_revision
    closed = False

    class StagedRuntime:
        service_urls: ClassVar[dict[str, str]] = {"workspace_api": "http://new"}

        def close(self) -> None:
            nonlocal closed
            closed = True

    async def prepare(
        *,
        configured: ConfiguredRemoteGateway,
        **_: object,
    ) -> tuple[RemoteGatewayConnection, StagedRuntime, list[dict[str, object]]]:
        if configured.connection_id == "rgw_offline":
            raise RuntimeError("远程 Gateway 离线")
        return (
            RemoteGatewayConnection(
                connection_id=configured.connection_id,
                name="New remote",
                host="new.example.com",
                port=22,
                username="developer",
                private_key_path=None,
                ssh_config_host="new",
                remote_gateway_port=8014,
                remote_gateway_id=_REMOTE_GATEWAY_ID,
                protocol_version=FEDERATION_PROTOCOL_VERSION,
                source_owner="config",
            ),
            StagedRuntime(),
            [
                {
                    'workspace_id': _OTHER_WORKSPACE_ID,
                    'name': 'New workspace',
                    'managed': False,
                    'services': ['workspace_api'],
                }
            ],
        )

    monkeypatch.setattr(remote_gateway_module, "_prepare_remote_gateway", prepare)
    with pytest.raises(RuntimeError, match="远程 Gateway 离线"):
        await reconcile_configured_remote_gateways(
            registry=registry,
            configured_workspaces=(
                ConfiguredRemoteGateway(
                    connection_id="rgw_configured",
                    host="new.example.com",
                    username="developer",
                    private_key_path="/tmp/key",
                ),
                ConfiguredRemoteGateway(
                    connection_id="rgw_offline",
                    host="offline.example.com",
                    username="developer",
                    private_key_path="/tmp/key",
                ),
            ),
            log_dir=tmp_path / "logs",
            health_request_timeout_seconds=2,
            health_poll_interval_seconds=0.01,
        )

    assert closed is True
    assert registry.registry_revision == initial_revision
    assert (
        registry.resolve(
            (old_connection.remote_gateway_id, _WORKSPACE_ID)
        ).backend_url
        == "http://old"
    )
    assert not registry.has_target(
        (_REMOTE_GATEWAY_ID, _OTHER_WORKSPACE_ID)
    )
    registry.close()
    state.close()


@pytest.mark.asyncio
async def test_remote_reconnect_refreshes_projection_cursor_and_retires_old_tunnel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    credential_store = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    )
    credential_store.issue(
        connection_id="rgw_reconnect",
        peer_gateway_id=_REMOTE_GATEWAY_ID,
    )
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
    old_connection = RemoteGatewayConnection(
        connection_id="rgw_reconnect",
        name="Remote",
        host="remote.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="remote",
        remote_gateway_port=8014,
        remote_gateway_id=_REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="config",
        remote_config_event_cursor=5,
    )

    class _Runtime:
        def __init__(self, url: str) -> None:
            self.service_urls = {"workspace_api": url}
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def request_terminate(self) -> None:
            self.closed = True

        def wait_closed(self) -> None:
            self.closed = True

    old_runtime = _Runtime("http://old")
    new_runtime = _Runtime("http://new")
    registry.apply_remote_projection_snapshot(
        connection=old_connection,
        runtime=old_runtime,
        projections=(
            WorkspaceTarget(
                gateway_id=_REMOTE_GATEWAY_ID,
                workspace_id=_WORKSPACE_ID,
                name="Old workspace",
                root_path=None,
                backend_url="http://old",
                connection_kind="remote_gateway",
                owner="remote_projection",
                remote_gateway_connection_ids=("rgw_reconnect",),
            ),
        ),
    )

    def pairing(**_: object):
        return credential_store.get("rgw_reconnect")

    async def start_tunnel(**_: object):
        return new_runtime

    async def discover(**_: object):
        return (
            {
                "protocol_version": FEDERATION_PROTOCOL_VERSION,
                "gateway_id": _REMOTE_GATEWAY_ID,
                "config_event_cursor": 8,
                "config_reload_state": "active",
                "config_reload_restart_required": False,
            },
            [
                {
                    'workspace_id': _OTHER_WORKSPACE_ID,
                    'name': 'New workspace',
                    'managed': True,
                    'connection_kind': 'local',
                    'services': ['workspace_api'],
                }
            ],
        )

    monkeypatch.setattr(
        remote_gateway_module, "obtain_pairing_credential_over_ssh", pairing
    )
    monkeypatch.setattr(
        remote_gateway_module, "start_remote_gateway_tunnel", start_tunnel
    )
    monkeypatch.setattr(remote_gateway_module, "discover_remote_gateway", discover)

    await remote_gateway_module.reconnect_remote_gateway(
        registry=registry,
        connection_id="rgw_reconnect",
        log_dir=tmp_path / "logs",
    )

    connection = registry.remote_gateway_connection("rgw_reconnect")
    assert connection.remote_config_event_cursor == 8
    assert connection.remote_config_state == "active"
    assert (
        registry.resolve(
            (_REMOTE_GATEWAY_ID, _OTHER_WORKSPACE_ID)
        ).backend_url
        == "http://new"
    )
    assert not registry.has_target(
        (_REMOTE_GATEWAY_ID, _WORKSPACE_ID)
    )
    assert old_runtime.closed is True
    assert new_runtime.closed is False
    assert (
        registry.resolve(
            (_REMOTE_GATEWAY_ID, _OTHER_WORKSPACE_ID)
        ).workspace_id
        == _OTHER_WORKSPACE_ID
    )
    registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health_workspace_id, expected_status",
    [
        (_WORKSPACE_ID, "ready"),
        (_OTHER_WORKSPACE_ID, "offline"),
    ],
)
async def test_workspace_dto_exposes_safe_remote_connection_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    health_workspace_id: str,
    expected_status: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    ).issue(
        connection_id="rgw_test",
        peer_gateway_id=_REMOTE_GATEWAY_ID,
    )
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
    registry.upsert_remote_gateway(
        RemoteGatewayConnection(
            connection_id="rgw_test",
            name="GPU server",
            host="remote.example.com",
            port=2222,
            username="developer",
            private_key_path="/secret/id_ed25519",
            ssh_config_host="gpu-server",
            remote_gateway_port=9014,
            remote_gateway_id=_REMOTE_GATEWAY_ID,
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            source_owner="manual",
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
    )
    registry.apply_remote_projection_snapshot(
        connection=registry.remote_gateway_connection("rgw_test"),
        projections=(WorkspaceTarget(
            gateway_id=_REMOTE_GATEWAY_ID,
            workspace_id=_WORKSPACE_ID,
            name="Remote workspace",
            root_path=None,
            backend_url="http://127.0.0.1:41000",
            connection_kind="remote_gateway",
            managed=True,
            remote_gateway_connection_ids=("rgw_test",),
            remote_service_names=("workspace_api",),
            owner="remote_projection",
        ),),
    )

    class WorkspaceHealthClient:
        def __init__(self, *, timeout: int, trust_env: bool) -> None:
            assert timeout == 2
            assert trust_env is False

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
        ) -> httpx.Response:
            assert headers["X-BoxTeam-Workspace-Id"] == _WORKSPACE_ID
            assert headers["X-BoxTeam-Target-Gateway-Id"] == _REMOTE_GATEWAY_ID
            assert headers["X-BoxTeam-Federation-Token"]
            payload: dict[str, object] = {
                "workspace_id": health_workspace_id,
            }
            if url.endswith("/api/v1/config/reload-status"):
                payload = {
                    "data": {
                        "healthy": True,
                        "restart_required": False,
                        "changed_sections": [],
                    }
                }
            return httpx.Response(
                200,
                json=payload,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", WorkspaceHealthClient)
    [workspace] = await registry.list_dtos()

    assert workspace.status == expected_status
    assert workspace.workspace_id == _WORKSPACE_ID
    assert workspace.gateway_id == _REMOTE_GATEWAY_ID
    assert not {"backend_workspace_id", "remote_workspace_id"} & workspace.model_dump().keys()
    if expected_status == "offline":
        assert workspace.services["workspace_api"].error == (
            f"工作区健康检查失败: workspace_id={_WORKSPACE_ID}: stage=workspace_health, error=RuntimeError"
        )
        assert any(
            record.exc_info and type(record.exc_info[1]) is RuntimeError
            and "backend UUID 不匹配: expected=00000000-0000-4000-8000-000000000002" in str(record.exc_info[1])
            for record in caplog.records
        )
        assert f"actual='{health_workspace_id}'" in caplog.text
    [remote_summary] = workspace.remote_gateways
    assert remote_summary.gateway_id == _REMOTE_GATEWAY_ID
    assert remote_summary.gateway_connection_id == "rgw_test"
    assert remote_summary.ssh_config_host == "gpu-server"
    assert remote_summary.host == "remote.example.com"
    assert remote_summary.port == 2222
    assert remote_summary.username == "developer"
    assert remote_summary.remote_gateway_port == 9014
    assert "private_key_path" not in remote_summary.model_dump()
    assert "/secret/id_ed25519" not in workspace.model_dump_json()


@pytest.mark.asyncio
async def test_discovery_filters_nested_workspaces_and_checks_protocol(
    tmp_path: Path,
    federation_client_state: _FederationClientState,
) -> None:
    store = FederationCredentialStore(storage_path=tmp_path / "federation.json")
    credential = store.issue(
        connection_id="rgw_test",
        peer_gateway_id="gateway_local",
    )
    credential = type(credential)(
        connection_id=credential.connection_id,
        peer_gateway_id=credential.peer_gateway_id,
        token="federation-secret",
        expires_at=credential.expires_at,
    )
    federation_client_state.manifest = {
        "protocol_version": FEDERATION_PROTOCOL_VERSION,
        "gateway_id": _REMOTE_GATEWAY_ID,
    }
    federation_client_state.workspaces = {
        "items": [
            {
                'workspace_id': _WORKSPACE_ID,
                'name': 'Direct',
                'connection_kind': 'local',
                'managed': True,
            },
            {
                'workspace_id': _OTHER_WORKSPACE_ID,
                'name': 'Nested',
                'connection_kind': 'remote_gateway',
                'managed': True,
            },
        ]
    }
    manifest, workspaces = await discover_remote_gateway(
        gateway_url="http://127.0.0.1:41000",
        credential=credential,
    )

    assert manifest["gateway_id"] == _REMOTE_GATEWAY_ID
    assert [item["workspace_id"] for item in workspaces] == [_WORKSPACE_ID]

    federation_client_state.manifest["protocol_version"] = 1
    with pytest.raises(RuntimeError, match=f"local={FEDERATION_PROTOCOL_VERSION}, remote=1"):
        await discover_remote_gateway(
            gateway_url="http://127.0.0.1:41000",
            credential=credential,
        )

    request_ids = [
        headers.get("X-Request-ID", "")
        for _url, headers in federation_client_state.requests
    ]
    assert len(request_ids) == 4
    assert all(request_ids)
    assert len(set(request_ids)) == 4

    federation_client_state.manifest = {
        "protocol_version": FEDERATION_PROTOCOL_VERSION,
        "gateway_id": "gateway_local",
    }
    with pytest.raises(RuntimeError, match="形成循环"):
        await discover_remote_gateway(
            gateway_url="http://127.0.0.1:41000",
            credential=credential,
        )


def test_remote_proxy_headers_preserve_request_id_and_hide_local_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path))
    store = FederationCredentialStore(
        storage_path=tmp_path / "credentials" / "federation.json"
    )
    credential = store.issue(
        connection_id="rgw_test",
        peer_gateway_id=_OTHER_GATEWAY_ID,
    )
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json", gateway_id=_OTHER_GATEWAY_ID,
    )
    registry.upsert_remote_gateway(RemoteGatewayConnection(
        connection_id="rgw_test", name="Remote", host="remote.example.com", port=22,
        username="developer", private_key_path=None, ssh_config_host=None,
        remote_gateway_port=8014, remote_gateway_id=_REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
    ))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/health",
            "app": SimpleNamespace(state=SimpleNamespace(registry=registry)),
            "headers": [
                (b"x-local-token", b"browser-token"),
                (b"x-request-id", b"untrusted"),
                (b"x-boxteam-gateway-id", b"forged-sender"),
                (b"x-boxteam-target-gateway-id", b"forged-target"),
                (b"x-boxteam-workspace-id", b"forged-workspace"),
            ],
        }
    )
    request.state.request_id = "req_authoritative"
    target = WorkspaceTarget(
        gateway_id=_REMOTE_GATEWAY_ID,
        workspace_id=_WORKSPACE_ID,
        name="Remote",
        root_path=None,
        backend_url="http://127.0.0.1:41000",
        connection_kind="remote_gateway",
        remote_gateway_connection_ids=("rgw_test",),
        owner="remote_projection",
    )

    headers = _proxy_headers(request, target, gateway_id="gateway_" + "b" * 32,
                             target_gateway_id="gateway_" + "a" * 32)

    assert headers["X-Request-ID"] == "req_authoritative"
    assert headers["X-BoxTeam-Gateway-Id"] == "gateway_" + "b" * 32
    assert headers["X-BoxTeam-Federation-Token"] == credential.token
    assert headers["X-BoxTeam-Workspace-Id"] == _WORKSPACE_ID
    assert headers["X-BoxTeam-Target-Gateway-Id"] == "gateway_" + "a" * 32
    assert "X-Local-Token" not in headers


def test_legacy_ssh_registry_has_explicit_migration_error(tmp_path: Path) -> None:
    storage_path = tmp_path / "workspaces.json"
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 13,
                "managed_backends": [],
                "targets": [
                    {
                        "workspace_id": "legacy",
                        "name": "Legacy",
                        "backend_url": "http://127.0.0.1:41000",
                        "connection_kind": "ssh",
                        "managed": True,
                        "backend_workspace_id": _WORKSPACE_ID,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="身份合同需要显式迁移"):
        GatewayWorkspaceRegistry(storage_path=storage_path)


def test_registry_rejects_legacy_source_owner(tmp_path: Path) -> None:
    """旧数据缺少或使用 legacy owner 时必须显式失败，不得默认归一。"""

    for payload_owner in (None, "legacy", "unknown"):
        storage_path = tmp_path / f"workspaces-{payload_owner}.json"
        connection: dict[str, object] = {
            "connection_id": "rgw_legacy",
            "name": "Legacy remote",
            "host": "remote.example.com",
            "port": 22,
            "username": "developer",
            "private_key_path": None,
            "ssh_config_host": "remote",
            "remote_gateway_port": 8014,
            "remote_gateway_id": "gateway_remote",
            "protocol_version": FEDERATION_PROTOCOL_VERSION,
        }
        if payload_owner is not None:
            connection["source_owner"] = payload_owner
        storage_path.write_text(
            json.dumps(
                {
                    "schema_version": _REGISTRY_SCHEMA_VERSION,
                    "managed_backends": [],
                    "remote_gateway_connections": [connection],
                    "targets": [],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="source_owner 非法"):
            GatewayWorkspaceRegistry(storage_path=storage_path)


@pytest.mark.asyncio
async def test_remote_gateway_uses_one_ssh_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Tunnel:
        process = object()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(
        "app.gateway.federation.allocate_ssh_tunnel_port",
        lambda: 41000,
    )

    def start_tunnel(**kwargs: object) -> _Tunnel:
        captured.update(kwargs)
        return _Tunnel()

    async def ready(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        "app.gateway.federation.start_ssh_tunnel_process",
        start_tunnel,
    )
    monkeypatch.setattr("app.gateway.federation.wait_for_http_ok", ready)
    connection = RemoteGatewayConnection(
        connection_id="rgw_test",
        name="Remote",
        host="remote.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="remote",
        remote_gateway_port=8014,
        remote_gateway_id=_REMOTE_GATEWAY_ID,
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual",
    )

    runtime = await start_remote_gateway_tunnel(
        connection=connection,
        log_dir=tmp_path,
    )

    forwards = captured["forwards"]
    assert isinstance(forwards, tuple)
    assert len(forwards) == 1
    assert forwards[0].remote_port == 8014
    assert runtime.service_urls == {"workspace_api": "http://127.0.0.1:41000"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["restarted", "blocked"])
async def test_remote_restart_is_delegated_with_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    credential = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    ).issue(connection_id="rgw_test", peer_gateway_id="gateway_local")
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
    registry.upsert_remote_gateway(
        RemoteGatewayConnection(
            connection_id="rgw_test", name="Remote", host="remote.example.com",
            port=22, username="developer", private_key_path=None, ssh_config_host="remote",
            remote_gateway_port=8014, remote_gateway_id=_REMOTE_GATEWAY_ID,
            protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        ),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://127.0.0.1:41000"}),
    )
    workspace_ids = [_WORKSPACE_ID, _OTHER_WORKSPACE_ID]
    generation = "remote-generation-2" if status == "restarted" else "remote-generation-1"
    remote_workspaces = []
    projections = []
    for index, workspace_id in enumerate(workspace_ids):
        projections.append(WorkspaceTarget(
            gateway_id=_REMOTE_GATEWAY_ID,
            workspace_id=workspace_id,
            name=f"Remote {index}",
            root_path=None,
            backend_url="http://127.0.0.1:41000",
            connection_kind="remote_gateway",
            owner="remote_projection",
            managed=True,
            remote_gateway_connection_ids=("rgw_test",),
            remote_backend_generation="remote-generation-1",
            remote_affected_workspace_ids=tuple(workspace_ids),
        ))
        remote_workspaces.append({
            'workspace_id': workspace_id,
            'name': f'Remote {index}',
            'managed': True,
            'backend_generation': generation,
            'affected_workspace_ids': workspace_ids,
            'services': ['workspace_api'],
        })
    registry.apply_remote_projection_snapshot(
        connection=registry.remote_gateway_connection("rgw_test"),
        projections=tuple(projections), activate=True,
    )
    captured: dict[str, object] = {}
    client_options: list[tuple[int, bool | None]] = []

    class RemoteRestartClient:
        def __init__(self, *, timeout: int, trust_env: bool | None = None) -> None:
            assert timeout in {2, 40}
            client_options.append((timeout, trust_env))

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(
            self, url: str, *, headers: dict[str, str], json: dict[str, object],
        ) -> httpx.Response:
            captured.update(url=url, headers=headers, body=json)
            return httpx.Response(
                200,
                json={"data": {
                    "gateway_id": _REMOTE_GATEWAY_ID, "workspace_id": _WORKSPACE_ID,
                    "status": status, "backend_generation": generation,
                    "affected_workspace_ids": workspace_ids,
                    "blockers": [{
                        "gateway_id": _REMOTE_GATEWAY_ID, "workspace_id": workspace_ids[1],
                        "kind": "proxy_stream",
                        "resource_id": "remote-stream",
                        "session_id": "gateway-proxy",
                        "status": "active",
                        "detail": "另一成员仍有 SSE",
                    }] if status == "blocked" else [],
                }},
                request=httpx.Request("POST", url),
            )

    async def discover_after_action(**kwargs):
        assert captured["url"]
        captured["refresh"] = kwargs
        return {
            "protocol_version": FEDERATION_PROTOCOL_VERSION,
            "config_event_cursor": 5,
        }, remote_workspaces

    original_list = GatewayWorkspaceRegistry.list_dtos
    async def unchecked_dtos(registry: GatewayWorkspaceRegistry):
        return await original_list(registry, check_health=False)

    monkeypatch.setattr("app.gateway.runtime.controller.httpx.AsyncClient", RemoteRestartClient)
    monkeypatch.setattr(remote_gateway_module, "discover_remote_gateway", discover_after_action)
    monkeypatch.setattr(GatewayWorkspaceRegistry, "list_dtos", unchecked_dtos)
    controller = GatewayWorkspaceRuntimeController(
        registry=registry, project_root=tmp_path, log_dir=tmp_path / "logs",
    )
    try:
        result = await controller.safe_restart_managed_backend(
            (_REMOTE_GATEWAY_ID, workspace_ids[0]), request_id="req_federation",
            group_request=GatewayRuntimeGroupRequest(
                expected_backend_generation="remote-generation-1",
                expected_workspace_ids=workspace_ids, desired_workspace_ids=workspace_ids,
            ),
        )
        assert (2, False) in client_options
        assert (result.gateway_id, result.workspace_id) == (_REMOTE_GATEWAY_ID, _WORKSPACE_ID)
        assert result.status == status
        assert result.backend_generation == generation
        assert result.affected_workspace_ids == workspace_ids
        assert captured["url"] == (
            f"http://127.0.0.1:41000/api/gateway/workspaces/{_WORKSPACE_ID}/runtime/restart-safe"
        )
        assert captured["headers"] == {
            "X-BoxTeam-Federation-Token": credential.token,
            "X-Request-ID": "req_federation",
            "X-BoxTeam-Gateway-Id": registry.gateway_id,
            "X-BoxTeam-Target-Gateway-Id": _REMOTE_GATEWAY_ID,
            "X-BoxTeam-Workspace-Id": _WORKSPACE_ID,
        }
        assert captured["body"] == {
            "expected_backend_generation": "remote-generation-1",
            "expected_workspace_ids": workspace_ids, "desired_workspace_ids": workspace_ids,
        }
        assert captured["refresh"]["expected_remote_gateway_id"] == _REMOTE_GATEWAY_ID
        assert all(item.remote_backend_generation == generation for item in registry.targets())
        assert all(item.remote_affected_workspace_ids == tuple(workspace_ids) for item in registry.targets())
        assert all(item.backend_generation == generation for item in result.workspaces.items)
        assert {(item.gateway_id, item.workspace_id) for item in result.blockers} == ({(_REMOTE_GATEWAY_ID, workspace_ids[1])} if status == "blocked" else set())
    finally:
        registry.close()


def test_pairing_ssh_has_bounded_timeout_and_stable_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH 配对必须带有限超时，超时收敛到既有联邦稳定错误码。"""

    captured: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(command, cast(float, kwargs.get("timeout")))

    monkeypatch.setattr("app.gateway.federation.subprocess.run", fake_run)

    with pytest.raises(FederationError) as error:
        obtain_pairing_credential_over_ssh(
            connection_id="rgw_test",
            local_gateway_id="gateway_local",
            host="remote.example.com",
            port=22,
            username="developer",
            private_key_path=None,
            ssh_config_host="developer-server",
        )

    assert captured["timeout"] == PAIRING_SSH_TIMEOUT_SECONDS
    assert error.value.code == "federation-deadline-exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace_id", [None, "workspace-alias", "00000000-0000-4000-8000-00000000000A"]
)
async def test_discovery_rejects_missing_or_noncanonical_workspace_uuid(
    tmp_path: Path,
    federation_client_state: _FederationClientState,
    workspace_id: str | None,
):
    credential = FederationCredentialStore(
        storage_path=tmp_path / "credentials.json"
    ).issue(
        connection_id="rgw_uuid",
        peer_gateway_id="gateway-local",
    )
    credential = type(credential)(
        connection_id=credential.connection_id,
        peer_gateway_id=credential.peer_gateway_id,
        token="federation-secret",
        expires_at=credential.expires_at,
    )
    federation_client_state.manifest = {
        "protocol_version": FEDERATION_PROTOCOL_VERSION,
        "gateway_id": _REMOTE_GATEWAY_ID,
    }
    federation_client_state.workspaces = {
        "items": [
            {
                'workspace_id': workspace_id,
                'name': 'Remote',
                'managed': False,
                'connection_kind': 'local',
            }
        ]
    }
    if workspace_id is None:
        del federation_client_state.workspaces["items"][0]["workspace_id"]
    with pytest.raises(ValueError, match="workspace_id"):
        await discover_remote_gateway(
            gateway_url="http://remote.test", credential=credential
        )


@pytest.mark.asyncio
async def test_federation_export_declares_registered_workspace_uuid(
    tmp_path, monkeypatch
):
    from app.gateway.routes.federation import federation_workspaces

    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway"))
    registry = GatewayWorkspaceRegistry(
        storage_path=tmp_path / "gateway/workspaces.json"
    )
    registry.upsert(
        WorkspaceTarget(
            gateway_id=registry.gateway_id,
            workspace_id=_WORKSPACE_ID,
            name="Local",
            root_path="/workspace",
            backend_url="http://local.test",
            connection_kind="local",
        )
    )
    response = await federation_workspaces(
        _=None, request_id="request-uuid-export", registry=registry
    )
    assert response.data is not None
    assert response.data.items[0].workspace_id == _WORKSPACE_ID
    assert response.data.gateway_id == registry.gateway_id
    assert not {"backend_workspace_id", "remote_workspace_id"} & response.data.items[0].model_dump().keys()
