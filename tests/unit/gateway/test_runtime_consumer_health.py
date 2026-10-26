from __future__ import annotations

from pathlib import Path

import pytest

from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime


class _FakeProcess:
    pid = 100_001
    def __init__(self, *, alive: bool = True) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive

    def request_terminate(self) -> None:
        self.alive = False

    def close(self, *, timeout_seconds: float = 8) -> None:
        del timeout_seconds
        self.alive = False

    def detach(self) -> None:
        return None


def _local_target() -> WorkspaceTarget:
    return WorkspaceTarget(
        workspace_id="local-workspace",
        name="Local workspace",
        root_path="/local",
        backend_url="http://127.0.0.1:41000",
        connection_kind="local",
        backend_workspace_id="00000000-0000-4000-8000-000000000001",
        owner="system",
        managed=True,
        system_default=True,
        removable=False,
    )


def _remote_connection() -> RemoteGatewayConnection:
    return RemoteGatewayConnection(
        connection_id="rgw_health",
        name="Health remote",
        host="remote.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="health",
        remote_gateway_port=8014,
        remote_gateway_id="remote-health",
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="config",
    )


def test_workspace_runtime_health_rejects_exited_owned_process(managed_backend_factory) -> None:
    process = _FakeProcess(alive=False)
    graph = managed_backend_factory((_local_target(),), backend_url="http://127.0.0.1:41000", process=process)
    runtime = graph.runtimes["local-workspace"]
    with pytest.raises(RuntimeError, match="managed Backend 未运行"):
        runtime.assert_healthy()


def test_registry_health_rejects_unhealthy_workspace_process(tmp_path: Path, managed_backend_factory) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    target = _local_target()
    registry.upsert(target)
    graph = managed_backend_factory((target,), backend_url=target.backend_url, process=_FakeProcess(alive=False))
    graph.publish(registry)
    with pytest.raises(RuntimeError, match="workspace_id=local-workspace"):
        registry.assert_runtime_consumers_healthy()
    registry.close()


def test_registry_health_rejects_configured_remote_projection_error(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    connection = _remote_connection()
    registry.upsert_remote_gateway(
        connection,
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote"}),
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id="projected-health",
            name="Projected health",
            root_path=None,
            backend_url="http://remote",
            connection_kind="remote_gateway",
            backend_workspace_id="00000000-0000-4000-8000-000000000002",
            owner="remote_projection",
            connection_id=connection.connection_id,
            remote_gateway_connection_id=connection.connection_id,
            remote_workspace_id="remote-workspace",
            connection_error="远程 Gateway 离线",
        )
    )

    with pytest.raises(RuntimeError, match="projection 不健康"):
        registry.assert_runtime_consumers_healthy()

    registry.close()
