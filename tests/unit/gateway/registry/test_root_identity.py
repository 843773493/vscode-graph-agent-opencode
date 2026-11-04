"""原注册表写入、重开与代次校验共用本地 UUID／物理根唯一约束。"""

import json
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceIdentityConflictError,
    WorkspaceTarget,
)


@pytest.fixture(params=["json", "sqlite"])
def root_registry(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[GatewayWorkspaceRegistry]:
    if request.param == "sqlite":
        with GatewayStateStore(path=tmp_path / "gateway.sqlite") as state:
            registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json", state_store=state)
            try:
                yield registry
            finally:
                registry.close()
    else:
        registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
        try:
            yield registry
        finally:
            registry.close()


@pytest.fixture
def bound_target(tmp_path: Path, root_registry: GatewayWorkspaceRegistry) -> WorkspaceTarget:
    root = tmp_path / "original"
    root.mkdir()
    return root_registry.upsert(WorkspaceTarget(
        workspace_id="registered-original", name="原工作区", root_path=str(root),
        backend_url="http://127.0.0.1:43100", connection_kind="local",
        backend_workspace_id=load_or_create_workspace_id(root),
    ))


def test_same_uuid_different_local_root_is_rejected_before_publication(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
) -> None:
    before = root_registry._capture_registry_state()
    candidate = replace(bound_target, workspace_id="conflicting-registration", root_path=str(tmp_path / "copy"),
                        target_generation="")
    with pytest.raises(WorkspaceIdentityConflictError, match="UUID 已绑定另一物理根") as error:
        root_registry.upsert(candidate)
    assert bound_target.backend_workspace_id is not None
    assert bound_target.backend_workspace_id in str(error.value)
    assert bound_target.workspace_id in str(error.value)
    assert root_registry._capture_registry_state() == before
    assert candidate.target_generation == "", "冲突不得进入新代次或发布阶段"
    assert root_registry.targets() == (bound_target,)
    assert root_registry.resolve().workspace_id == bound_target.workspace_id


def test_same_local_root_can_update_without_reassigning_identity(
    root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
) -> None:
    updated = root_registry.upsert(replace(bound_target, name="改名后"))
    assert updated.backend_workspace_id == bound_target.backend_workspace_id
    assert updated.root_path == bound_target.root_path
    assert root_registry.targets() == (updated,)


def test_different_native_gateway_can_export_same_uuid(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
) -> None:
    for index in (1, 2):
        connection = RemoteGatewayConnection(
            connection_id=f"connection-{index}", name=f"远端 {index}", host="example.test", port=22,
            username="test", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
            remote_gateway_id=f"gateway-{index}", protocol_version=1, source_owner="manual",
        )
        root_registry.upsert_remote_gateway(connection)
        root_registry.upsert(replace(bound_target, workspace_id=f"projection-{index}", root_path=None,
            connection_kind="remote_gateway", owner="remote_projection", connection_id=connection.connection_id,
            remote_gateway_connection_id=connection.connection_id, remote_workspace_id="remote-original"))
    assert len(root_registry.targets()) == 3


def test_persisted_conflicting_root_is_rejected_without_repair(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
) -> None:
    path = root_registry._storage_path
    state = root_registry._state_store
    duplicate = replace(bound_target, workspace_id="copied-registration", root_path=str(tmp_path / "copy"))
    payload = state.load_workspace_registry() if state is not None else json.loads(path.read_bytes())
    assert payload is not None
    targets = payload["targets"]
    assert isinstance(targets, list)
    targets.append(asdict(duplicate))
    if state is None:
        path.write_text(json.dumps(payload))
        original = path.read_bytes()
    else:
        state.replace_workspace_registry(payload, expected_revision=root_registry.registry_revision, owner="manual")
        original = state.load_workspace_registry()
    with pytest.raises(WorkspaceIdentityConflictError, match="UUID 已绑定另一物理根"):
        GatewayWorkspaceRegistry(storage_path=path, state_store=state)
    assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == original
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize("has_identity", [False, True])
def test_new_root_registration_initializes_once_and_preserves_existing_identity(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, has_identity: bool,
) -> None:
    root = tmp_path / "new-root"
    expected = load_or_create_workspace_id(root) if has_identity else None
    actual = root_registry.resolve_local_workspace_identity(Path.cwd(), root, create_directory=True)
    assert expected is None or actual == expected
    identity = workspace_identity_path(root)
    before = identity.read_bytes(), identity.stat().st_mtime_ns, identity.stat().st_ino
    target = root_registry.upsert(WorkspaceTarget(
        workspace_id="new-registration", name="新登记", root_path=str(root),
        backend_workspace_id=actual, backend_url="", connection_kind="local",
    ))
    assert root_registry.resolve_local_workspace_identity(Path.cwd(), root, create_directory=True) == actual
    assert (identity.read_bytes(), identity.stat().st_mtime_ns, identity.stat().st_ino) == before
    assert root_registry.resolve(target.workspace_id).backend_workspace_id == actual
