import asyncio
import json
import re
from pathlib import Path

import pytest

from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.federation import build_projected_workspace_id
from app.gateway.workspace_ids import build_workspace_id
from app.gateway.workspace_ids import build_managed_local_workspace_id


class _HealthResponse:
    status_code = 200


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
    def __init__(self, *, timeout: int) -> None:
        assert timeout == 2

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def get(self, url: str, *, headers: dict[str, str]):
        assert headers == {"X-Local-Token": "local-dev-token"}
        if url.endswith("/api/v1/config/reload-status"):
            return _ConfigStatusResponse()
        return _HealthResponse()


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

    with pytest.raises(ValueError, match="SSH 直连后端注册记录"):
        GatewayWorkspaceRegistry(storage_path=storage_path)


def test_registry_v5_migrates_managed_local_id_to_stable_root_id(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "gateway.json"
    root_path = "/workspace/managed"
    old_id = build_workspace_id(
        "local",
        root_path,
        "http://127.0.0.1:41000",
    )
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "active_workspace_id": old_id,
                "targets": [
                    {
                        "workspace_id": old_id,
                        "name": "Managed",
                        "root_path": root_path,
                        "backend_url": "http://127.0.0.1:41000",
                        "connection_kind": "local",
                        "managed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = GatewayWorkspaceRegistry(storage_path=storage_path)
    expected_id = build_managed_local_workspace_id(root_path)

    assert registry.active_workspace_id == expected_id
    assert registry.resolve(expected_id).name == "Managed"
    persisted = json.loads(storage_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 7
    assert persisted["active_workspace_id"] == expected_id


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
