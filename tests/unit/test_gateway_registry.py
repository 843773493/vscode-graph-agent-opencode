import asyncio
import json
import re
from pathlib import Path

import pytest

from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    SshWorkspaceConnection,
    WorkspaceTarget,
)
from app.gateway.service_runtime import WorkspaceRuntime
from app.gateway.service_types import RemoteServiceSpec
from app.gateway.workspace_ids import build_ssh_workspace_id, build_workspace_id


class _HealthResponse:
    status_code = 200


class _ConcurrentHealthClient:
    active_requests = 0
    peak_requests = 0

    def __init__(self, *, timeout: int) -> None:
        assert timeout == 2

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        assert url.endswith("/api/v1/health")
        assert headers == {"X-Local-Token": "local-dev-token"}
        type(self).active_requests += 1
        type(self).peak_requests = max(
            type(self).peak_requests,
            type(self).active_requests,
        )
        await asyncio.sleep(0.02)
        type(self).active_requests -= 1
        return _HealthResponse()


class _AllServicesHealthClient:
    def __init__(self, *, timeout: int) -> None:
        assert timeout == 2

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        assert url.endswith(("/api/v1/health", "/health"))
        assert headers == {"X-Local-Token": "local-dev-token"}
        return _HealthResponse()


def test_gateway_workspace_ids_use_32_hex_characters():
    local_id = build_workspace_id(
        "local",
        "/workspace/project",
        "http://127.0.0.1:8010",
    )
    ssh_id = build_ssh_workspace_id(
        root_path="/workspace/project",
        host="remote.example.com",
        port=22,
        username="developer",
        remote_backend_host="127.0.0.1",
        remote_backend_port=8010,
    )

    assert re.fullmatch(r"gw_[0-9a-f]{32}", local_id)
    assert re.fullmatch(r"gw_[0-9a-f]{32}", ssh_id)
    assert local_id != ssh_id


def test_registry_migrates_legacy_workspace_ids_and_active_reference(tmp_path: Path):
    storage_path = tmp_path / "gateway.json"
    local_legacy_id = "gw_0123456789ab"
    ssh_legacy_id = "gw_abcdef012345"
    payload = {
        "active_workspace_id": ssh_legacy_id,
        "order_customized": True,
        "targets": [
            {
                "workspace_id": local_legacy_id,
                "name": "Local",
                "root_path": "/workspace/local",
                "backend_url": "http://127.0.0.1:8010",
                "connection_kind": "local",
                "managed": False,
            },
            {
                "workspace_id": ssh_legacy_id,
                "name": "Remote",
                "root_path": "/workspace/remote",
                "backend_url": "http://127.0.0.1:41000",
                "connection_kind": "ssh",
                "managed": True,
                "ssh_connection": {
                    "host": "remote.example.com",
                    "port": 2222,
                    "username": "developer",
                    "private_key_path": "/home/user/.ssh/remote_ed25519",
                    "remote_backend_host": "127.0.0.1",
                    "remote_backend_port": 8010,
                },
            },
        ],
    }
    storage_path.write_text(json.dumps(payload), encoding="utf-8")

    registry = GatewayWorkspaceRegistry(storage_path=storage_path)

    workspace_ids = [target.workspace_id for target in registry.targets()]
    assert all(re.fullmatch(r"gw_[0-9a-f]{32}", item) for item in workspace_ids)
    assert registry.active_workspace_id == workspace_ids[1]
    assert [target.name for target in registry.targets()] == ["Local", "Remote"]
    persisted = json.loads(storage_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 4
    assert persisted["active_workspace_id"] == workspace_ids[1]
    assert local_legacy_id not in storage_path.read_text(encoding="utf-8")
    assert ssh_legacy_id not in storage_path.read_text(encoding="utf-8")
    assert storage_path.stat().st_mode & 0o777 == 0o600


@pytest.fixture
def registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    result = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    for index in range(3):
        result.upsert(
            WorkspaceTarget(
                workspace_id=f"workspace-{index}",
                name=f"Workspace {index}",
                root_path=f"/tmp/workspace-{index}",
                backend_url=f"http://127.0.0.1:{8100 + index}",
                connection_kind="local",
            ),
            runtime=WorkspaceRuntime(
                service_urls={
                    "workspace_api": f"http://127.0.0.1:{8100 + index}"
                }
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
    assert all(item.status == "ready" for item in result)
    assert result[0].services["workspace_api"].local_port == 8100
    assert result[0].services["workspace_api"].health_path == "/api/v1/health"
    assert result[0].services["terminal_manager"].status == "unavailable"
    assert _ConcurrentHealthClient.peak_requests == 3


@pytest.mark.asyncio
async def test_list_dtos_exposes_ssh_forward_and_remote_service_ports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="workspace-ssh",
            name="Remote",
            root_path="/workspace/project",
            backend_url="http://127.0.0.1:41000",
            connection_kind="ssh",
            managed=True,
            ssh_connection=SshWorkspaceConnection(
                host="remote.example.com",
                port=2222,
                username="developer",
                private_key_path="/home/user/.ssh/remote_ed25519",
                remote_backend_host="127.0.0.1",
                remote_backend_port=8010,
                remote_services=(
                    RemoteServiceSpec("workspace_api", "127.0.0.1", 8010, True),
                    RemoteServiceSpec("terminal_manager", "127.0.0.1", 8012),
                    RemoteServiceSpec("browser_manager", "127.0.0.1", 8015),
                ),
            ),
        ),
        runtime=WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:41000",
                "terminal_manager": "http://127.0.0.1:41001",
                "browser_manager": "http://127.0.0.1:41002",
            }
        ),
    )
    monkeypatch.setattr(
        "app.gateway.registry.httpx.AsyncClient",
        _AllServicesHealthClient,
    )

    result = (await registry.list_dtos())[0]

    browser = result.services["browser_manager"]
    assert browser.status == "ready"
    assert browser.local_url == "http://127.0.0.1:41002"
    assert browser.local_port == 41002
    assert browser.remote_host == "127.0.0.1"
    assert browser.remote_port == 8015
    assert browser.health_path == "/health"


def test_registry_persists_complete_ssh_reconnect_state(tmp_path: Path):
    storage_path = tmp_path / "gateway.json"
    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    registry.upsert(
        WorkspaceTarget(
            workspace_id="workspace-ssh",
            name="Remote",
            root_path="/workspace/project",
            backend_url="http://127.0.0.1:41000",
            connection_kind="ssh",
            managed=True,
            ssh_connection=SshWorkspaceConnection(
                host="remote.example.com",
                port=2222,
                username="developer",
                private_key_path="/home/user/.ssh/remote_ed25519",
                remote_backend_host="127.0.0.1",
                remote_backend_port=8010,
                remote_services=(
                    RemoteServiceSpec(
                        name="workspace_api",
                        host="127.0.0.1",
                        port=8010,
                        required=True,
                    ),
                    RemoteServiceSpec(
                        name="terminal_manager",
                        host="127.0.0.1",
                        port=8012,
                    ),
                    RemoteServiceSpec(
                        name="browser_manager",
                        host="127.0.0.1",
                        port=8015,
                    ),
                ),
            ),
        )
    )

    restored = GatewayWorkspaceRegistry(storage_path=storage_path)
    target = restored.resolve("workspace-ssh")

    assert target.ssh_connection is not None
    assert target.ssh_connection.private_key_path == "/home/user/.ssh/remote_ed25519"
    assert [
        service.name for service in target.ssh_connection.remote_services
    ] == ["workspace_api", "terminal_manager", "browser_manager"]
    assert restored.active_workspace_id == "workspace-ssh"
    assert storage_path.stat().st_mode & 0o777 == 0o600
    assert storage_path.read_bytes().endswith(b"\n")
