"""远端身份预检沿原联邦入口核验，缓存或同 UUID 不能代替原路由来源。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import pytest

from app.gateway import federation
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from configs import workspace_identity_migration as migration
from configs.cli import main

WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"
OTHER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000003"
REMOTE_GATEWAY_ID = "gateway_" + "a" * 32


@dataclass
class ProbeRuntime(WorkspaceRuntime):
    closed: bool = False
    close_error: BaseException | None = None

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@dataclass
class RemotePreflight:
    root: Path
    registry: GatewayWorkspaceRegistry
    credentials: FederationCredentialStore
    payloads: dict[str, list[dict[str, object]]]
    manifests: dict[str, dict[str, object]]
    requests: list[httpx.Request] = field(default_factory=list)
    runtimes: list[ProbeRuntime] = field(default_factory=list)
    inventory_metadata: dict[str, dict[str, object]] = field(default_factory=dict)
    close_error: BaseException | None = None

    def preview(self) -> migration.WorkspaceIdentityPreview:
        return migration.preview_workspace_identity_migration(gateway_root=self.root, project_root=Path.cwd())

    def originals(self) -> tuple[dict[str, bytes], str]:
        files = {path.relative_to(self.root).as_posix(): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file() and not path.name.startswith("gateway.sqlite")}
        with sqlite3.connect(self.root / "gateway.sqlite") as connection:
            return files, "\n".join(connection.iterdump())


@pytest.fixture
def remote_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RemotePreflight]:
    root = tmp_path / "home/state/gateway"
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    gateway_id = load_or_create_gateway_id(root / "identity.json")
    credentials = FederationCredentialStore(storage_path=root / "credentials/federation.json")
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        registry = GatewayWorkspaceRegistry(storage_path=root / "workspaces.json", state_store=store)
        state = RemotePreflight(root, registry, credentials, {}, {})
        for index in range(2):
            connection_id = f"rgw_remote_{index}"
            owner = REMOTE_GATEWAY_ID
            credentials.issue(connection_id=connection_id, peer_gateway_id=gateway_id)
            registry.upsert_remote_gateway(RemoteGatewayConnection(
                connection_id=connection_id, name="不可公开的连接名称", host="host-sentinel",
                port=22, username="username-sentinel", private_key_path="/private/key-sentinel",
                ssh_config_host=None, remote_gateway_port=8014, remote_gateway_id=owner,
                protocol_version=1, source_owner="manual",
            ))
            registry.upsert(WorkspaceTarget(
                workspace_id=f"gw_projection_{index}", name="投影", root_path=None,
                backend_url="http://127.0.0.1:49997", connection_kind="remote_gateway",
                owner="remote_projection", connection_id=connection_id, remote_gateway_connection_id=connection_id,
                remote_workspace_id="gw_native", backend_workspace_id=WORKSPACE_ID,
            ))
            state.manifests[connection_id] = {"protocol_version": 1, "gateway_id": owner}
            state.payloads[connection_id] = [{
                "workspace_id": "gw_native", "backend_workspace_id": WORKSPACE_ID,
                "name": "原生工作区", "managed": True, "connection_kind": "local",
            }]

        async def tunnel(*, connection: RemoteGatewayConnection, log_dir: Path) -> WorkspaceRuntime:
            assert connection.host == "host-sentinel"
            assert log_dir.is_relative_to(tmp_path)
            runtime = ProbeRuntime(service_urls={"workspace_api": f"http://{connection.connection_id}.invalid"}, close_error=state.close_error)
            state.runtimes.append(runtime)
            return runtime

        def respond(request: httpx.Request) -> httpx.Response:
            state.requests.append(request)
            assert request.url.host is not None
            connection_id = request.url.host.removesuffix(".invalid")
            expected = credentials.get(connection_id)
            assert request.headers["X-BoxTeam-Federation-Token"] == expected.token
            data = (state.manifests[connection_id] if request.url.path.endswith("/manifest")
                    else {"items": state.payloads[connection_id], **state.inventory_metadata.get(connection_id, {
                        "gateway_id": REMOTE_GATEWAY_ID, "protocol_version": 1,
                        "identities_verified": request.url.params.get("verify_identity") == "true",
                    })})
            return httpx.Response(200, json={"data": data})

        original_client = httpx.AsyncClient
        monkeypatch.setattr(migration, "start_remote_gateway_tunnel", tunnel)
        monkeypatch.setattr(federation.httpx, "AsyncClient", partial(original_client, transport=httpx.MockTransport(respond)))
        try:
            yield state
        finally:
            registry.close()


def test_cli_remote_verification_keeps_source_and_connection_identity_without_writes(
    remote_preflight: RemotePreflight, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    before = remote_preflight.originals()
    monkeypatch.setattr("sys.argv", ["boxteam", "preview-workspace-identity", "--verify-remote"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["mapping_complete"] is True
    assert report["apply_available"] is False
    assert report["blockers"] == []
    assert [(item["gateway_id"], item["workspace_id"], item["connection_id"], item["verified"])
            for item in report["mappings"]] == [(REMOTE_GATEWAY_ID, WORKSPACE_ID, f"rgw_remote_{i}", True) for i in range(2)]
    assert len(remote_preflight.requests) == 4
    assert len({request.headers["X-Request-ID"] for request in remote_preflight.requests}) == 4
    assert len(remote_preflight.runtimes) == 2
    assert all(runtime.closed for runtime in remote_preflight.runtimes)
    assert "sentinel" not in json.dumps(report)
    assert all(credential.token not in json.dumps(report) for credential in remote_preflight.credentials.list_all())
    assert remote_preflight.originals() == before


def test_default_cli_stays_offline_and_does_not_claim_remote_confirmation(
    remote_preflight: RemotePreflight, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    before = remote_preflight.originals()
    monkeypatch.setattr("sys.argv", ["boxteam", "preview-workspace-identity"])
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in report["blockers"]] == ["remote_identity_unverified"] * 2
    assert remote_preflight.requests == remote_preflight.runtimes == []
    assert remote_preflight.originals() == before


def test_cli_expands_the_same_gateway_root_for_snapshot_and_credentials(
    remote_preflight: RemotePreflight, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    original_expanduser = Path.expanduser

    def expanduser(path: Path) -> Path:
        return remote_preflight.root if path == Path("~/selected-gateway") else original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", "~/selected-gateway")
    monkeypatch.setattr("sys.argv", ["boxteam", "preview-workspace-identity", "--verify-remote"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["blockers"] == []
    assert report["mapping_complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [
    ("uuid_changed", "remote_workspace_identity_mismatch"),
    ("route_missing", "remote_workspace_missing"),
    ("duplicate_route", "remote_identity_probe_failed"),
    ("duplicate_uuid", "remote_identity_probe_failed"),
    ("gateway_changed", "remote_identity_probe_failed"),
    ("protocol_changed", "remote_identity_probe_failed"),
    ("invalid_uuid", "remote_identity_probe_failed"),
    ("no_live_proof", "remote_identity_probe_failed"),
    ("wrong_inventory_owner", "remote_identity_probe_failed"),
    ("nonboolean_live_proof", "remote_identity_probe_failed"),
    ("missing_credential", "remote_identity_probe_failed"),
    ("expired_credential", "remote_identity_probe_failed"),
    ("wrong_credential_owner", "remote_identity_probe_failed"),
])
async def test_remote_failure_blocks_only_its_original_reference_and_closes_tunnel(
    remote_preflight: RemotePreflight, failure: str, code: str,
) -> None:
    state = remote_preflight
    item = state.payloads["rgw_remote_0"][0]
    if failure == "uuid_changed":
        item["backend_workspace_id"] = OTHER_WORKSPACE_ID
        state.payloads["rgw_remote_0"].append({**item, "workspace_id": "other", "backend_workspace_id": WORKSPACE_ID})
    elif failure == "route_missing":
        item["workspace_id"] = "different_route_same_uuid"
    elif failure.startswith("duplicate_"):
        state.payloads["rgw_remote_0"].append({**item, "workspace_id": "other"} if failure == "duplicate_uuid" else dict(item))
    elif failure == "gateway_changed":
        state.manifests["rgw_remote_0"]["gateway_id"] = "gateway_changed"
    elif failure == "protocol_changed":
        state.manifests["rgw_remote_0"]["protocol_version"] = 999
    elif failure == "invalid_uuid":
        item["backend_workspace_id"] = "not-a-uuid"
    elif failure in {"no_live_proof", "wrong_inventory_owner", "nonboolean_live_proof"}:
        state.inventory_metadata["rgw_remote_0"] = (
            {} if failure == "no_live_proof" else {"gateway_id": REMOTE_GATEWAY_ID if failure == "nonboolean_live_proof" else "gateway_wrong",
                                                    "protocol_version": 1, "identities_verified": 1 if failure == "nonboolean_live_proof" else True}
        )
    elif failure == "missing_credential":
        state.credentials.remove("rgw_remote_0")
    else:
        credential = state.credentials.get("rgw_remote_0")
        state.credentials.put(
            replace(credential, expires_at=datetime.now(UTC) - timedelta(seconds=1)) if failure == "expired_credential"
            else replace(credential, peer_gateway_id="gateway_wrong")
        )
    before = state.originals()
    result = await migration.verify_remote_workspace_identities(state.preview(), gateway_root=state.root, log_dir=state.root / "logs")
    assert [(item.code, item.source) for item in result.blockers] == [(code, "gw_projection_0")]
    assert [item.verified for item in result.mappings] == [False, True]
    assert all(runtime.closed for runtime in state.runtimes)
    assert len(state.runtimes) == (1 if "credential" in failure else 2)
    assert state.originals() == before


@pytest.mark.asyncio
async def test_verification_uses_frozen_connections_and_routes_instead_of_reloading_registry(
    remote_preflight: RemotePreflight,
) -> None:
    state = remote_preflight
    preview = state.preview()
    state.registry.upsert(replace(state.registry.resolve("gw_projection_0"), remote_workspace_id="gw_changed_after_preview"))
    state.registry.upsert_remote_gateway(replace(state.registry.remote_gateway_connection("rgw_remote_0"), host="changed-after-preview"))
    before = state.originals()
    result = await migration.verify_remote_workspace_identities(preview, gateway_root=state.root, log_dir=state.root / "logs")
    assert result.registry_digest == preview.registry_digest
    assert result.registry_revision == preview.registry_revision
    assert result.blockers == ()
    assert [item.legacy_workspace_id for item in result.mappings] == ["gw_projection_0", "gw_projection_1"]
    assert all(item.verified for item in result.mappings)
    assert state.originals() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("close_failure", [False, True])
async def test_cancelled_discovery_closes_owned_tunnel_and_propagates_cancellation(
    remote_preflight: RemotePreflight, monkeypatch: pytest.MonkeyPatch, close_failure: bool,
) -> None:
    cancellation = asyncio.CancelledError("调用方取消核验")
    cleanup_error = OSError("隧道关闭失败") if close_failure else None
    remote_preflight.close_error = cleanup_error

    async def cancelled(**_kwargs: object) -> tuple[dict[str, object], list[dict[str, object]]]:
        raise cancellation

    monkeypatch.setattr(migration, "discover_remote_gateway", cancelled)
    with pytest.raises(asyncio.CancelledError) as failure:
        await migration.verify_remote_workspace_identities(remote_preflight.preview(), gateway_root=remote_preflight.root,
                                                          log_dir=remote_preflight.root / "logs")
    assert failure.value is cancellation
    assert failure.value.__cause__ is cleanup_error
    assert len(remote_preflight.runtimes) == 1
    assert remote_preflight.runtimes[0].closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("close_failure", [False, True])
async def test_cancelled_tunnel_health_check_closes_original_ssh_process(
    remote_preflight: RemotePreflight, monkeypatch: pytest.MonkeyPatch, close_failure: bool,
) -> None:
    cleanup_error = OSError("隧道关闭失败") if close_failure else None
    @dataclass
    class Tunnel:
        process: None = None
        closed: bool = False

        def close(self) -> None:
            self.closed = True
            if cleanup_error is not None:
                raise cleanup_error

    tunnel = Tunnel()
    cancellation = asyncio.CancelledError("健康检查取消")

    async def cancelled(*_args: object, **_kwargs: object) -> None:
        raise cancellation

    monkeypatch.setattr(federation, "allocate_ssh_tunnel_port", lambda: 49996)
    monkeypatch.setattr(federation, "start_ssh_tunnel_process", lambda **_kwargs: tunnel)
    monkeypatch.setattr(federation, "wait_for_http_ok", cancelled)
    connection = remote_preflight.preview().remote_probes[0].connection
    with pytest.raises(asyncio.CancelledError) as failure:
        await federation.start_remote_gateway_tunnel(connection=connection, log_dir=remote_preflight.root / "logs")
    assert failure.value is cancellation
    assert failure.value.__cause__ is cleanup_error
    assert tunnel.closed is True
