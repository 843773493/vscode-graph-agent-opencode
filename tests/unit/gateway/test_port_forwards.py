from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.gateway.auth import verify_gateway_token
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.main import app
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.schemas.gateway import CreatePortForwardRequest
from app.gateway.server.port_forwarding import get_port_forward_manager


class _FakeManagedProcess:
    def __init__(self) -> None:
        self.process = SimpleNamespace(poll=lambda: None)
        self.closed = False

    def close(self, *, timeout_seconds: float = 8) -> None:
        del timeout_seconds
        self.closed = True


def _registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert_remote_gateway(
        RemoteGatewayConnection(
            connection_id="rgw_server",
            name="Development server",
            host="server.example.com",
            port=22,
            username="developer",
            private_key_path="/keys/never-persist-content",
            ssh_config_host="development-alias",
            remote_gateway_port=8014,
            remote_gateway_id="remote-gateway",
            protocol_version=1,
        )
    )
    for workspace_id, root_path in (
        ("workspace_a", "/srv/a"),
        ("workspace_b", "/srv/b"),
    ):
        registry.upsert(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=workspace_id,
                root_path=root_path,
                backend_url="http://127.0.0.1:41000",
                connection_kind="remote_gateway",
                remote_gateway_connection_id="rgw_server",
                remote_workspace_id=f"remote_{workspace_id}",
            ),
            activate=False,
        )
    return registry


def _manager(
    tmp_path: Path,
    registry: GatewayWorkspaceRegistry,
    calls: list[dict[str, object]],
    processes: list[_FakeManagedProcess],
) -> SshPortForwardManager:
    def starter(**kwargs: object):
        calls.append(kwargs)
        process = _FakeManagedProcess()
        processes.append(process)
        return process, tmp_path / "logs" / f"{kwargs['forward_id']}.log"

    manager = SshPortForwardManager(
        registry=registry,
        storage_path=tmp_path / "port-forwards.json",
        log_dir=tmp_path / "logs",
        process_starter=starter,
        port_allocator=lambda: 41234,
    )

    async def ready(*_: object) -> None:
        return None

    manager._wait_until_listening = ready  # type: ignore[method-assign]
    return manager


@pytest.mark.asyncio
async def test_create_binds_forward_to_workspace_and_reuses_ssh_alias(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), calls, processes)

    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(
            remote_port=5173,
            protocol="http",
            label="Vite",
        ),
    )

    assert created.workspace_id == "workspace_a"
    assert created.connection_id == "rgw_server"
    assert created.local_port == 41234
    assert created.local_url == "http://127.0.0.1:41234"
    assert created.status == "active"
    assert calls[0]["ssh_config_host"] == "development-alias"
    assert calls[0]["host"] == "server.example.com"
    assert calls[0]["username"] == "developer"
    forward = calls[0]["forward"]
    assert forward.remote_host == "127.0.0.1"
    assert forward.remote_port == 5173

    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"][0]["workspace_id"] == "workspace_a"
    assert persisted["items"][0]["desired_running"] is True
    assert "private_key" not in json.dumps(persisted)
    if os.name != "nt":
        assert (tmp_path / "port-forwards.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_server_remote_port_has_single_workspace_owner(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41235),
    )

    with pytest.raises(ValueError, match="已归属于工作区 workspace_a"):
        await manager.create(
            "workspace_b",
            CreatePortForwardRequest(remote_port=5173, local_port=41236),
        )


@pytest.mark.asyncio
async def test_change_local_port_restarts_same_forward_and_persists_new_port(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), calls, processes)
    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41235),
    )

    changed = await manager.change_local_port(
        "workspace_a",
        created.forward_id,
        41236,
    )

    assert changed.forward_id == created.forward_id
    assert changed.local_port == 41236
    assert changed.local_url == "http://127.0.0.1:41236"
    assert processes[0].closed is True
    assert calls[1]["forward"].local_port == 41236
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"][0]["local_port"] == 41236


@pytest.mark.asyncio
async def test_change_label_replaces_definition_and_persists_display_name(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41236, label="旧名称"),
    )

    changed = await manager.change_label("workspace_a", created.forward_id, "新名称")

    assert changed.label == "新名称"
    assert (await manager.list("workspace_a"))[0].label == "新名称"
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"][0]["label"] == "新名称"


@pytest.mark.asyncio
async def test_local_port_conflict_is_rejected_across_workspaces(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41237),
    )

    with pytest.raises(ValueError, match="本地端口 .* 已被转发"):
        await manager.create(
            "workspace_b",
            CreatePortForwardRequest(remote_port=8080, local_port=41237),
        )


@pytest.mark.asyncio
async def test_delete_closes_process_before_removing_persisted_definition(
    tmp_path: Path,
) -> None:
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, _registry(tmp_path), [], processes)
    created = await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41238),
    )

    await manager.delete("workspace_a", created.forward_id)

    assert processes[0].closed is True
    assert await manager.list("workspace_a") == []
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"] == []


@pytest.mark.asyncio
async def test_reconcile_removes_forward_owned_by_stale_projection(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    processes: list[_FakeManagedProcess] = []
    manager = _manager(tmp_path, registry, [], processes)
    await manager.create(
        "workspace_a",
        CreatePortForwardRequest(remote_port=5173, local_port=41241),
    )

    registry.remove("workspace_a")
    await manager.reconcile_workspaces()

    assert processes[0].closed is True
    persisted = json.loads(
        (tmp_path / "port-forwards.json").read_text(encoding="utf-8")
    )
    assert persisted["items"] == []


@pytest.mark.asyncio
async def test_restore_failure_remains_visible_on_workspace_list(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "port-forwards.json"
    storage_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    {
                        "forward_id": "pf_stale",
                        "workspace_id": "workspace_a",
                        "connection_id": "rgw_replaced",
                        "remote_host": "127.0.0.1",
                        "remote_port": 5173,
                        "local_host": "127.0.0.1",
                        "local_port": 41239,
                        "protocol": "http",
                        "label": "Vite",
                        "desired_running": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = SshPortForwardManager(
        registry=_registry(tmp_path),
        storage_path=storage_path,
        log_dir=tmp_path / "logs",
    )

    await manager.restore()
    restored = await manager.list("workspace_a")

    assert restored[0].status == "error"
    assert "绑定的远程 Gateway 已变化" in (restored[0].error or "")


def test_load_rejects_duplicate_persisted_local_port(tmp_path: Path) -> None:
    items = [
        {
            "forward_id": f"pf_{index}",
            "workspace_id": f"workspace_{index}",
            "connection_id": "rgw_server",
            "remote_host": "127.0.0.1",
            "remote_port": 5000 + index,
            "local_host": "127.0.0.1",
            "local_port": 41240,
            "protocol": "tcp",
            "label": None,
            "desired_running": True,
        }
        for index in range(2)
    ]
    storage_path = tmp_path / "port-forwards.json"
    storage_path.write_text(
        json.dumps({"schema_version": 1, "items": items}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="本地端口重复"):
        SshPortForwardManager(
            registry=_registry(tmp_path),
            storage_path=storage_path,
            log_dir=tmp_path / "logs",
        )


@pytest.mark.asyncio
async def test_local_workspace_is_rejected(tmp_path: Path) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    registry.upsert(
        WorkspaceTarget(
            workspace_id="local",
            name="Local",
            root_path=str(tmp_path),
            backend_url="http://127.0.0.1:8010",
            connection_kind="local",
        )
    )
    manager = SshPortForwardManager(
        registry=registry,
        storage_path=tmp_path / "port-forwards.json",
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(ValueError, match="只支持远程 Gateway 投影工作区"):
        await manager.create(
            "local",
            CreatePortForwardRequest(remote_port=5173),
        )


def test_create_api_returns_complete_workspace_forward_list(tmp_path: Path) -> None:
    manager = _manager(tmp_path, _registry(tmp_path), [], [])
    app.dependency_overrides[verify_gateway_token] = lambda: "test-token"
    app.dependency_overrides[get_port_forward_manager] = lambda: manager
    try:
        response = TestClient(app).post(
            "/api/gateway/workspaces/workspace_a/port-forwards",
            headers={"X-Request-ID": "req-port-forward"},
            json={
                "remote_port": 5173,
                "local_port": 41241,
                "protocol": "https",
                "label": "Preview",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["request_id"] == "req-port-forward"
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["workspace_id"] == "workspace_a"
    assert items[0]["connection_id"] == "rgw_server"
    assert items[0]["status"] == "active"
    assert items[0]["local_url"] == "https://127.0.0.1:41241"
