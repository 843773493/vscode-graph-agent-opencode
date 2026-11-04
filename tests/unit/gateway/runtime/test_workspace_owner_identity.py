"""完整组与远端运行时使用原生 Gateway/UUID，不建立投影别名。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
)
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway.lifecycle import GatewayRuntimeGroupRequest

LOCAL_GATEWAY = "gateway_00000000000040008000000000000001"
REMOTE_GATEWAY = "gateway_00000000000040008000000000000002"
OTHER_GATEWAY = "gateway_00000000000040008000000000000003"
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
OTHER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def owner_registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=LOCAL_GATEWAY)
    registry.upsert(WorkspaceTarget(
        gateway_id=LOCAL_GATEWAY, workspace_id=WORKSPACE_ID, name="本机", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:41000", connection_kind="local",
    ))
    for index, gateway_id in enumerate((REMOTE_GATEWAY, OTHER_GATEWAY), start=1):
        connection = RemoteGatewayConnection(
            connection_id=f"remote-{index}", name=f"远端 {index}", host="127.0.0.1", port=22,
            username="fixture", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
            remote_gateway_id=gateway_id, protocol_version=2, source_owner="manual",
        )
        target = WorkspaceTarget(
            gateway_id=gateway_id, workspace_id=WORKSPACE_ID, name=f"远端 {index}", root_path=None,
            backend_url=f"http://127.0.0.1:{41000 + index}", connection_kind="remote_gateway",
            managed=True, owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,),
            remote_service_names=("workspace_api",),
        )
        registry.apply_remote_projection_snapshot(
            connection=connection, projections=(target,),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": target.backend_url}),
        )
    return registry


@pytest.fixture
def owner_controller(owner_registry: GatewayWorkspaceRegistry, tmp_path: Path) -> GatewayWorkspaceRuntimeController:
    return GatewayWorkspaceRuntimeController(registry=owner_registry, project_root=tmp_path, log_dir=tmp_path / "logs")


def test_managed_contract_serializes_one_gateway_and_only_uuid_members() -> None:
    contract = ManagedBackendContract(
        gateway_id=LOCAL_GATEWAY, owner_id="owner", generation="generation",
        members=(ManagedBackendMember(WORKSPACE_ID),), backend_url="",
    )
    assert contract.to_payload() == {
        "gateway_id": LOCAL_GATEWAY, "owner_id": "owner", "generation": "generation",
        "members": [{"workspace_id": WORKSPACE_ID}], "backend_url": "", "debug_port": None,
    }
    with pytest.raises(RuntimeError):
        ManagedBackendContract(
            gateway_id=LOCAL_GATEWAY, owner_id="owner", generation="generation",
            members=(ManagedBackendMember("route-alias"),), backend_url="",
        )


def test_remote_group_request_preserves_uuid_and_checks_native_owner(
    owner_registry: GatewayWorkspaceRegistry, owner_controller: GatewayWorkspaceRuntimeController,
) -> None:
    target = owner_registry.resolve((REMOTE_GATEWAY, WORKSPACE_ID))
    request = GatewayRuntimeGroupRequest(expected_backend_generation="generation", expected_workspace_ids=[WORKSPACE_ID])
    assert owner_controller._remote_group_request(target, request) == request.model_dump()
    unknown = request.model_copy(update={"desired_workspace_ids": [OTHER_WORKSPACE_ID]})
    with pytest.raises(LookupError):
        owner_controller._remote_group_request(target, unknown)
    assert owner_registry.resolve(WORKSPACE_ID).gateway_id == LOCAL_GATEWAY


@pytest.mark.asyncio
async def test_delegated_result_preserves_owner_and_rejects_cross_gateway_blockers(
    owner_registry: GatewayWorkspaceRegistry, owner_controller: GatewayWorkspaceRuntimeController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refreshed(**_: object) -> None:
        return None

    async def listed():
        return []

    monkeypatch.setattr("app.gateway.runtime.controller.refresh_remote_gateway_projections", refreshed)
    monkeypatch.setattr(owner_registry, "list_dtos", listed)
    target = owner_registry.resolve((REMOTE_GATEWAY, WORKSPACE_ID))
    data = {
        "gateway_id": REMOTE_GATEWAY, "workspace_id": WORKSPACE_ID, "status": "blocked",
        "affected_workspace_ids": [WORKSPACE_ID], "blockers": [{
            "gateway_id": REMOTE_GATEWAY, "workspace_id": WORKSPACE_ID, "kind": "job",
            "resource_id": "job-1", "session_id": "session-1", "status": "running",
        }],
    }
    result = await owner_controller._delegated_result(target, data)
    assert (result.gateway_id, result.workspace_id) == (REMOTE_GATEWAY, WORKSPACE_ID)
    assert result.affected_workspace_ids == [WORKSPACE_ID]
    assert result.blockers[0].gateway_id == REMOTE_GATEWAY
    assert result.workspaces.active_workspace is not None
    assert result.workspaces.active_workspace.gateway_id == LOCAL_GATEWAY
    data["blockers"][0]["gateway_id"] = OTHER_GATEWAY
    with pytest.raises(RuntimeError, match="Gateway 归属不匹配"):
        await owner_controller._delegated_result(target, data)


@pytest.fixture
def forward_path(tmp_path: Path) -> Path:
    path = tmp_path / "forwards.json"
    path.write_text(json.dumps({"schema_version": 2, "items": [{
        "forward_id": f"forward-{index}", "gateway_id": gateway_id, "workspace_id": WORKSPACE_ID,
        "connection_id": f"remote-{index}", "remote_host": "127.0.0.1", "remote_port": 8000,
        "local_host": "127.0.0.1", "local_port": 42000 + index, "protocol": "http",
        "label": None, "desired_running": False,
    } for index, gateway_id in enumerate((REMOTE_GATEWAY, OTHER_GATEWAY), start=1)]}))
    return path


@pytest.mark.asyncio
async def test_port_forwards_with_same_uuid_remain_owned_by_native_gateway(
    owner_registry: GatewayWorkspaceRegistry, forward_path: Path, tmp_path: Path,
) -> None:
    manager = SshPortForwardManager(registry=owner_registry, storage_path=forward_path, log_dir=tmp_path / "logs")
    first, = await manager.list((REMOTE_GATEWAY, WORKSPACE_ID))
    second, = await manager.list((OTHER_GATEWAY, WORKSPACE_ID))
    assert first.gateway_id == REMOTE_GATEWAY and first.forward_id == "forward-1"
    assert second.gateway_id == OTHER_GATEWAY and second.forward_id == "forward-2"
    with pytest.raises(ValueError, match="只支持远程"):
        await manager.list(WORKSPACE_ID)
    with pytest.raises(LookupError, match="端口转发不存在"):
        await manager.delete((OTHER_GATEWAY, WORKSPACE_ID), first.forward_id)
    assert len(json.loads(forward_path.read_text())["items"]) == 2


def test_nonempty_legacy_port_forwards_require_explicit_migration_without_rewrite(
    owner_registry: GatewayWorkspaceRegistry, forward_path: Path, tmp_path: Path,
) -> None:
    payload = json.loads(forward_path.read_text())
    payload["schema_version"] = 1
    for item in payload["items"]:
        item.pop("gateway_id")
        item["workspace_id"] = "legacy-route-alias"
    forward_path.write_text(json.dumps(payload))
    before = forward_path.read_bytes()
    with pytest.raises(RuntimeError, match="workspace-identity 显式迁移"):
        SshPortForwardManager(registry=owner_registry, storage_path=forward_path, log_dir=tmp_path / "logs")
    assert forward_path.read_bytes() == before
