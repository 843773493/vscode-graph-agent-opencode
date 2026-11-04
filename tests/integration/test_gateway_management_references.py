"""真实 registry 与 WorkspaceRuntime 的管理引用集成，外部隧道使用 ProcessProbe。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.workspace import WorkspaceRuntime
from tests.support.gateway_managed_backends import ProcessProbe

LOCAL = "gateway_" + "a" * 32
REMOTE = "gateway_" + "b" * 32
CONNECTION = "rgw_management"


@pytest.fixture
def management_registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    return GatewayWorkspaceRegistry(gateway_id=LOCAL, storage_path=tmp_path / "workspaces.json")


@pytest.fixture
def connection() -> RemoteGatewayConnection:
    return RemoteGatewayConnection(
        connection_id=CONNECTION, name="远端", host="remote.test", port=22, username="tester",
        private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id=REMOTE, protocol_version=2, source_owner="manual",
    )


@pytest.fixture
def tunnel() -> tuple[WorkspaceRuntime, ProcessProbe]:
    process = ProcessProbe("ssh_tunnel")
    return WorkspaceRuntime(service_urls={"workspace_api": "http://remote.test"}, processes={"ssh_tunnel": process}), process


def test_management_reference_retains_connection_without_workspaces(
    management_registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection,
    tunnel: tuple[WorkspaceRuntime, ProcessProbe],
) -> None:
    registry = management_registry
    runtime, process = tunnel
    registry.upsert_remote_gateway(connection, runtime=runtime)
    assert registry.acquire_remote_gateway_reference(CONNECTION) == "http://remote.test"
    with pytest.raises(RuntimeError, match="管理请求引用"):
        registry.remove_remote_gateway(CONNECTION)
    registry.release_remote_gateway_reference(CONNECTION)
    assert registry.remote_gateway_connection(CONNECTION) == connection
    assert registry.remote_gateway_url(CONNECTION) == "http://remote.test"
    assert not process.closed
    registry.remove_remote_gateway(CONNECTION)
    assert process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]


def test_last_management_reference_closes_only_retired_tunnel(
    management_registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection,
    tunnel: tuple[WorkspaceRuntime, ProcessProbe],
) -> None:
    registry = management_registry
    original, process = tunnel
    registry.upsert_remote_gateway(connection, runtime=original)
    registry.acquire_remote_gateway_reference(CONNECTION)
    registry.acquire_remote_gateway_reference(CONNECTION)
    replacement = WorkspaceRuntime(service_urls={"workspace_api": "http://replacement.test"})
    registry.upsert_remote_gateway(connection, runtime=replacement)
    assert not process.closed
    registry.release_remote_gateway_reference(CONNECTION)
    assert not process.closed
    registry.release_remote_gateway_reference(CONNECTION)
    assert process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]
    assert registry.remote_gateway_url(CONNECTION) == "http://replacement.test"
    with pytest.raises(RuntimeError, match="重复释放"):
        registry.release_remote_gateway_reference(CONNECTION)


def test_batch_promotion_keeps_occupied_retired_tunnel_until_release(
    management_registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection,
    tunnel: tuple[WorkspaceRuntime, ProcessProbe],
) -> None:
    registry = management_registry
    original, process = tunnel
    configured = replace(connection, source_owner="config")
    registry.upsert_remote_gateway(configured, runtime=original)
    registry.acquire_remote_gateway_reference(CONNECTION)
    replacement = WorkspaceRuntime(service_urls={"workspace_api": "http://replacement.test"})
    batch = registry.apply_remote_projection_batch(
        connections=(configured,), runtimes={CONNECTION: replacement},
        projections={CONNECTION: ()}, defer_retiring_runtimes=True,
    )
    batch.promote()
    assert batch.finished and not process.closed
    registry.release_remote_gateway_reference(CONNECTION)
    assert process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]
    assert registry.remote_gateway_url(CONNECTION) == "http://replacement.test"


@pytest.mark.parametrize("replace_staged", [False, True])
def test_batch_rollback_retains_inflight_staged_and_retired_tunnels(
    management_registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection,
    tunnel: tuple[WorkspaceRuntime, ProcessProbe], replace_staged: bool,
) -> None:
    registry = management_registry
    original, original_process = tunnel
    configured = replace(connection, source_owner="config")
    registry.upsert_remote_gateway(configured, runtime=original)
    staged_process = ProcessProbe("staged_tunnel")
    staged = WorkspaceRuntime(service_urls={"workspace_api": "http://staged.test"}, processes={"ssh_tunnel": staged_process})
    batch = registry.apply_remote_projection_batch(
        connections=(configured,), runtimes={CONNECTION: staged},
        projections={CONNECTION: ()}, defer_retiring_runtimes=True,
    )
    assert registry.acquire_remote_gateway_reference(CONNECTION) == "http://staged.test"
    replaced_process = ProcessProbe("replacement_tunnel")
    if replace_staged:
        registry.upsert_remote_gateway(configured, runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://replacement.test"}, processes={"ssh_tunnel": replaced_process},
        ))
    batch.rollback()
    assert batch.finished and registry.remote_gateway_url(CONNECTION) == "http://remote.test"
    assert not original_process.closed and not staged_process.closed and not replaced_process.closed
    registry.release_remote_gateway_reference(CONNECTION)
    assert staged_process.events == ["terminate:staged_tunnel", "close:staged_tunnel"]
    if replace_staged:
        assert replaced_process.events == ["terminate:replacement_tunnel", "close:replacement_tunnel"]
    assert not original_process.closed
    assert registry.remote_gateway_url(CONNECTION) == "http://remote.test"


@pytest.mark.parametrize("decision", ["promote", "rollback"])
def test_request_release_preserves_previous_runtime_until_batch_decision(
    management_registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection,
    tunnel: tuple[WorkspaceRuntime, ProcessProbe], decision: str,
) -> None:
    registry = management_registry
    original, original_process = tunnel
    configured = replace(connection, source_owner="config")
    registry.upsert_remote_gateway(configured, runtime=original)
    registry.acquire_remote_gateway_reference(CONNECTION)
    staged_process = ProcessProbe("staged_tunnel")
    staged = WorkspaceRuntime(service_urls={"workspace_api": "http://staged.test"}, processes={"ssh_tunnel": staged_process})
    batch = registry.apply_remote_projection_batch(
        connections=(configured,), runtimes={CONNECTION: staged},
        projections={CONNECTION: ()}, defer_retiring_runtimes=True,
    )
    registry.release_remote_gateway_reference(CONNECTION)
    assert not original_process.closed and not staged_process.closed
    if decision == "promote":
        batch.promote()
        assert original_process.events == ["terminate:ssh_tunnel", "close:ssh_tunnel"]
        assert not staged_process.closed
        assert registry.remote_gateway_url(CONNECTION) == "http://staged.test"
    else:
        batch.rollback()
        assert not original_process.closed
        assert staged_process.events == ["terminate:staged_tunnel", "close:staged_tunnel"]
        assert registry.remote_gateway_url(CONNECTION) == "http://remote.test"


@pytest.mark.parametrize("unavailable", ["error", "missing-runtime", "exited-process"])
def test_unavailable_connection_never_acquires_reference(
    management_registry: GatewayWorkspaceRegistry, connection: RemoteGatewayConnection,
    tunnel: tuple[WorkspaceRuntime, ProcessProbe], unavailable: str,
) -> None:
    registry = management_registry
    runtime, process = tunnel
    if unavailable == "error":
        connection = replace(connection, connection_error="离线")
    if unavailable == "exited-process":
        process.alive = False
    registry.upsert_remote_gateway(connection, runtime=None if unavailable == "missing-runtime" else runtime)
    with pytest.raises((LookupError, RuntimeError)):
        registry.acquire_remote_gateway_reference(CONNECTION)
    with pytest.raises(RuntimeError, match="重复释放"):
        registry.release_remote_gateway_reference(CONNECTION)
