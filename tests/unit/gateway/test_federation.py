from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    RemoteGatewayConnection,
    build_projected_workspace_id,
    discover_remote_gateway,
    start_remote_gateway_tunnel,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.server.workspace_proxy import _proxy_headers
from app.gateway.main import _inbound_gateway_access_list


def _response(data: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": data},
        request=httpx.Request("GET", "http://remote.test"),
    )


class _FederationClient:
    manifest: dict[str, object]
    workspaces: dict[str, object]

    def __init__(self, *, timeout: float) -> None:
        assert timeout == 10

    async def __aenter__(self) -> "_FederationClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        assert headers["X-BoxTeam-Federation-Token"] == "federation-secret"
        if url.endswith("/manifest"):
            return _response(self.manifest)
        return _response(self.workspaces)


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
    registry = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json"
    )

    async def list_dtos() -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                workspace_id="local_workspace",
                name="Local workspace",
                root_path=str(tmp_path / "local"),
                status="ready",
                managed=True,
                system_default=True,
                connection_kind="local",
            ),
            SimpleNamespace(
                workspace_id="remote_projection",
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
    assert [workspace.workspace_id for workspace in result.items] == [
        "local_workspace"
    ]


def test_gateway_local_credential_is_generated_and_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path))

    first = get_gateway_local_token()
    second = get_gateway_local_token()

    assert first == second
    assert first != "local-dev-token"
    assert (tmp_path / "credentials" / "local-token").stat().st_mode & 0o777 == 0o600


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
        remote_gateway_id="gateway_remote",
        protocol_version=FEDERATION_PROTOCOL_VERSION,
    )
    registry.upsert_remote_gateway(
        connection,
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id=build_projected_workspace_id("rgw_test", "remote_ws"),
            name="Remote workspace",
            root_path="/srv/workspace",
            backend_url="http://127.0.0.1:41000",
            connection_kind="remote_gateway",
            managed=True,
            remote_gateway_connection_id="rgw_test",
            remote_workspace_id="remote_ws",
        )
    )

    serialized = storage_path.read_text(encoding="utf-8")
    assert "federation-secret" not in serialized
    restored = GatewayWorkspaceRegistry(storage_path=storage_path)
    target = restored.targets()[0]
    assert target.remote_gateway_connection_id == "rgw_test"
    assert target.remote_workspace_id == "remote_ws"


@pytest.mark.asyncio
async def test_workspace_dto_exposes_safe_remote_connection_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    ).issue(
        connection_id="rgw_test",
        peer_gateway_id="gateway_remote",
    )
    registry = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json"
    )
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
            remote_gateway_id="gateway_remote",
            protocol_version=FEDERATION_PROTOCOL_VERSION,
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id="projected",
            name="Remote workspace",
            root_path="/srv/workspace",
            backend_url="http://127.0.0.1:41000",
            connection_kind="remote_gateway",
            managed=True,
            remote_gateway_connection_id="rgw_test",
            remote_workspace_id="remote_ws",
        )
    )

    class _Client:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 2

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
        ) -> httpx.Response:
            assert headers["X-BoxTeam-Federation-Token"]
            payload: dict[str, object] = {}
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

    monkeypatch.setattr("app.gateway.registry.httpx.AsyncClient", _Client)
    [workspace] = await registry.list_dtos()

    assert workspace.remote is not None
    assert workspace.remote.ssh_config_host == "gpu-server"
    assert workspace.remote.host == "remote.example.com"
    assert workspace.remote.port == 2222
    assert workspace.remote.username == "developer"
    assert workspace.remote.remote_gateway_port == 9014
    assert "private_key_path" not in workspace.remote.model_dump()


@pytest.mark.asyncio
async def test_discovery_filters_nested_workspaces_and_checks_protocol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    _FederationClient.manifest = {
        "protocol_version": FEDERATION_PROTOCOL_VERSION,
        "gateway_id": "gateway_remote",
    }
    _FederationClient.workspaces = {
        "items": [
            {
                "workspace_id": "direct",
                "name": "Direct",
                "root_path": "/direct",
                "connection_kind": "local",
                "managed": True,
            },
            {
                "workspace_id": "nested",
                "name": "Nested",
                "root_path": "/nested",
                "connection_kind": "remote_gateway",
                "managed": True,
            },
        ]
    }
    monkeypatch.setattr("app.gateway.federation.httpx.AsyncClient", _FederationClient)

    manifest, workspaces = await discover_remote_gateway(
        gateway_url="http://127.0.0.1:41000",
        credential=credential,
    )

    assert manifest["gateway_id"] == "gateway_remote"
    assert [item["workspace_id"] for item in workspaces] == ["direct"]

    _FederationClient.manifest["protocol_version"] = 2
    with pytest.raises(RuntimeError, match="local=1, remote=2"):
        await discover_remote_gateway(
            gateway_url="http://127.0.0.1:41000",
            credential=credential,
        )

    _FederationClient.manifest = {
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
        peer_gateway_id="gateway_local",
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/health",
            "headers": [
                (b"x-local-token", b"browser-token"),
                (b"x-request-id", b"untrusted"),
            ],
        }
    )
    request.state.request_id = "req_authoritative"
    target = WorkspaceTarget(
        workspace_id="projected",
        name="Remote",
        root_path="/remote",
        backend_url="http://127.0.0.1:41000",
        connection_kind="remote_gateway",
        remote_gateway_connection_id="rgw_test",
        remote_workspace_id="remote_ws",
    )

    headers = _proxy_headers(request, target)

    assert headers["X-Request-ID"] == "req_authoritative"
    assert headers["X-BoxTeam-Federation-Token"] == credential.token
    assert headers["X-BoxTeam-Workspace-Id"] == "remote_ws"
    assert "X-Local-Token" not in headers


def test_legacy_ssh_registry_has_explicit_migration_error(tmp_path: Path) -> None:
    storage_path = tmp_path / "workspaces.json"
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "targets": [
                    {
                        "workspace_id": "legacy",
                        "name": "Legacy",
                        "root_path": "/remote",
                        "backend_url": "http://127.0.0.1:41000",
                        "connection_kind": "ssh",
                        "managed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SSH 直连后端注册记录"):
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

    async def ready(*_: object) -> None:
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
        remote_gateway_id="gateway_remote",
        protocol_version=FEDERATION_PROTOCOL_VERSION,
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
async def test_remote_restart_is_delegated_with_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    credential = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    ).issue(
        connection_id="rgw_test",
        peer_gateway_id="gateway_local",
    )
    registry = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json"
    )
    registry.upsert_remote_gateway(
        RemoteGatewayConnection(
            connection_id="rgw_test",
            name="Remote",
            host="remote.example.com",
            port=22,
            username="developer",
            private_key_path=None,
            ssh_config_host="remote",
            remote_gateway_port=8014,
            remote_gateway_id="gateway_remote",
            protocol_version=FEDERATION_PROTOCOL_VERSION,
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41000"}
        ),
    )
    target = WorkspaceTarget(
        workspace_id="projected",
        name="Remote workspace",
        root_path="/remote",
        backend_url="http://127.0.0.1:41000",
        connection_kind="remote_gateway",
        managed=True,
        remote_gateway_connection_id="rgw_test",
        remote_workspace_id="remote_ws",
    )
    registry.upsert(target)
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == 40

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
        ) -> httpx.Response:
            captured["url"] = url
            captured["headers"] = headers
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "restarted",
                        "blockers": [],
                    }
                },
                request=httpx.Request("POST", url),
            )

    async def empty_dtos(_: GatewayWorkspaceRegistry) -> list[object]:
        return []

    monkeypatch.setattr(
        "app.gateway.runtime.controller.httpx.AsyncClient",
        _Client,
    )
    monkeypatch.setattr(GatewayWorkspaceRegistry, "list_dtos", empty_dtos)
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )

    result = await controller.safe_restart_managed_backend(
        "projected",
        request_id="req_federation",
    )

    assert result.status == "restarted"
    assert captured["url"] == (
        "http://127.0.0.1:41000/api/gateway/workspaces/"
        "remote_ws/runtime/restart-safe"
    )
    assert captured["headers"] == {
        "X-BoxTeam-Federation-Token": credential.token,
        "X-Request-ID": "req_federation",
    }
