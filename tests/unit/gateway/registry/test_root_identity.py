"""原生根绑定与远端快照共用 canonical 工作区 owner，冷恢复拒绝损坏。"""

import json
import shutil
from collections.abc import Iterator
from contextlib import ExitStack, closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceIdentityConflictError,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime


@pytest.fixture(params=["json", "sqlite"])
def root_registry(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[GatewayWorkspaceRegistry]:
    with ExitStack() as resources:
        state = resources.enter_context(GatewayStateStore(path=tmp_path / "gateway.sqlite")) if request.param == "sqlite" else None
        registry = resources.enter_context(closing(GatewayWorkspaceRegistry(
            storage_path=tmp_path / "workspaces.json", state_store=state,
        )))
        yield registry


@pytest.fixture
def bound_target(tmp_path: Path, root_registry: GatewayWorkspaceRegistry) -> WorkspaceTarget:
    root = tmp_path / "original"
    workspace_id = root_registry.resolve_local_workspace_identity(Path.cwd(), root, create_directory=True)
    return root_registry.upsert(WorkspaceTarget(
        gateway_id=root_registry.gateway_id, workspace_id=workspace_id, name="原工作区", root_path=str(root),
        backend_url="http://127.0.0.1:43100", connection_kind="local",
    ))


def test_same_uuid_different_local_root_is_rejected_before_publication(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
) -> None:
    before = root_registry._capture_registry_state()
    path, state = root_registry._storage_path, root_registry._state_store
    persisted = state.load_workspace_registry() if state is not None else path.read_bytes()
    assert bound_target.root_path is not None
    identity = workspace_identity_path(Path(bound_target.root_path))
    identity_before = identity.read_bytes()
    candidate = replace(bound_target, root_path=str(tmp_path / "copy"), target_generation="")
    with pytest.raises(WorkspaceIdentityConflictError, match="UUID 已绑定另一物理根") as error:
        root_registry.upsert(candidate)
    assert bound_target.workspace_id in str(error.value)
    assert bound_target.root_path is not None and bound_target.root_path in str(error.value)
    assert candidate.root_path is not None and candidate.root_path in str(error.value)
    assert root_registry._capture_registry_state() == before
    assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == persisted
    assert candidate.target_generation == "", "冲突不得进入新代次或发布阶段"
    assert not (tmp_path / "copy").exists()
    assert identity.read_bytes() == identity_before
    assert root_registry.targets() == (bound_target,)
    assert root_registry.resolve().key == bound_target.key


def test_same_local_root_can_update_without_reassigning_identity(
    root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
) -> None:
    updated = root_registry.upsert(replace(bound_target, name="改名后"))
    assert updated.name == "改名后"
    assert updated.key == bound_target.key
    assert updated.root_path == bound_target.root_path
    assert updated.target_generation == bound_target.target_generation
    assert updated.runtime_lease_id == bound_target.runtime_lease_id
    assert root_registry.targets() == (updated,)
    assert root_registry.resolve(updated.key) == updated
    assert root_registry.resolve(updated.workspace_id) == updated
    with closing(GatewayWorkspaceRegistry(
        storage_path=root_registry._storage_path, state_store=root_registry._state_store,
        gateway_id=root_registry.gateway_id,
    )) as reopened:
        assert reopened.resolve(updated.key) == updated


@pytest.mark.parametrize("create_directory", [False, True])
def test_registration_identity_read_rejects_uuid_bound_to_another_local_root(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
    create_directory: bool,
) -> None:
    assert bound_target.root_path is not None
    original_root = Path(bound_target.root_path)
    copied_root = tmp_path / "copied-root"
    shutil.copytree(original_root, copied_root)
    original_identity = workspace_identity_path(original_root)
    copied_identity = workspace_identity_path(copied_root)
    identities_before = original_identity.read_bytes(), copied_identity.read_bytes()
    before = root_registry._capture_registry_state()
    path, state = root_registry._storage_path, root_registry._state_store
    persisted = state.load_workspace_registry() if state is not None else path.read_bytes()
    with pytest.raises(WorkspaceIdentityConflictError, match="UUID 已绑定另一物理根") as error:
        root_registry.resolve_local_workspace_identity(Path.cwd(), copied_root, create_directory=create_directory)
    assert bound_target.workspace_id in str(error.value)
    assert str(original_root) in str(error.value) and str(copied_root) in str(error.value)
    assert root_registry._capture_registry_state() == before
    assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == persisted
    assert (original_identity.read_bytes(), copied_identity.read_bytes()) == identities_before


@pytest.mark.parametrize("entry", ["snapshot", "batch"])
def test_different_native_gateway_can_export_same_uuid(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
    entry: Literal["snapshot", "batch"],
) -> None:
    connections = tuple(RemoteGatewayConnection(
            connection_id=f"connection-{index}", name=f"远端 {index}", host="example.test", port=22,
            username="test", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
            remote_gateway_id="gateway_" + str(index) * 32, protocol_version=FEDERATION_PROTOCOL_VERSION,
            source_owner="manual" if entry == "snapshot" else "config", remote_config_event_cursor=index,
        ) for index in (1, 2))
    projections = {connection.connection_id: (WorkspaceTarget(
        gateway_id=connection.remote_gateway_id, workspace_id=bound_target.workspace_id,
        name=connection.name, root_path=None, backend_url="", connection_kind="remote_gateway",
        owner="remote_projection", remote_gateway_connection_ids=(connection.connection_id,),
    ),) for connection in connections}
    if entry == "snapshot":
        for connection in connections:
            root_registry.apply_remote_projection_snapshot(
                connection=connection, projections=projections[connection.connection_id],
            )
    else:
        handle = root_registry.apply_remote_projection_batch(
            connections=connections, projections=projections,
            runtimes={connection.connection_id: WorkspaceRuntime(
                service_urls={"workspace_api": f"http://127.0.0.1:{43200 + index}"},
            ) for index, connection in enumerate(connections)},
        )
        handle.promote()
    keys = {bound_target.key} | {(connection.remote_gateway_id, bound_target.workspace_id) for connection in connections}
    assert {target.key for target in root_registry.targets()} == keys
    assert {target.workspace_id for target in root_registry.targets()} == {bound_target.workspace_id}
    assert root_registry.active_workspace == bound_target.key
    assert root_registry.resolve(bound_target.workspace_id) == bound_target
    for connection in connections:
        target = root_registry.resolve((connection.remote_gateway_id, bound_target.workspace_id))
        assert target.root_path is None and target.owner == "remote_projection"
        assert target.remote_gateway_connection_ids == (connection.connection_id,)
        assert target.target_generation and target.runtime_lease_id
        assert root_registry.remote_gateway_connection(connection.connection_id) == connection
    with closing(GatewayWorkspaceRegistry(
        storage_path=tmp_path / "workspaces.json", state_store=root_registry._state_store,
        gateway_id=root_registry.gateway_id,
    )) as reopened:
        assert reopened._capture_registry_state() == root_registry._capture_registry_state()


def test_remote_uuid_does_not_prevent_native_root_registration(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry,
) -> None:
    root = tmp_path / "native-root"
    workspace_id = root_registry.resolve_local_workspace_identity(Path.cwd(), root, create_directory=True)
    identity = workspace_identity_path(root)
    original_identity = identity.read_bytes(), identity.stat().st_mtime_ns, identity.stat().st_ino
    connection = RemoteGatewayConnection(
        connection_id="connection-remote-first", name="先登记远端", host="example.test", port=22,
        username="test", private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
        remote_gateway_id="gateway_" + "1" * 32, protocol_version=FEDERATION_PROTOCOL_VERSION,
        source_owner="manual", remote_config_event_cursor=1,
    )
    root_registry.apply_remote_projection_snapshot(connection=connection, projections=(WorkspaceTarget(
        gateway_id=connection.remote_gateway_id, workspace_id=workspace_id, name="远端同 UUID",
        root_path=None, backend_url="", connection_kind="remote_gateway", owner="remote_projection",
        remote_gateway_connection_ids=(connection.connection_id,),
    ),), activate=True)
    remote_key = connection.remote_gateway_id, workspace_id
    remote_before = asdict(root_registry.resolve(remote_key))
    assert root_registry.has_target(remote_key)
    assert not root_registry.has_target(workspace_id)
    assert root_registry.resolve_local_workspace_identity(Path.cwd(), root, create_directory=True) == workspace_id
    native = root_registry.upsert(WorkspaceTarget(
        gateway_id=root_registry.gateway_id, workspace_id=workspace_id, name="本机同 UUID",
        root_path=str(root), backend_url="", connection_kind="local",
    ))
    assert {target.key for target in root_registry.targets()} == {native.key, remote_key}
    assert root_registry.resolve(workspace_id) == native
    assert asdict(root_registry.resolve(remote_key)) == remote_before
    assert root_registry.remote_gateway_connection(connection.connection_id) == connection
    assert (identity.read_bytes(), identity.stat().st_mtime_ns, identity.stat().st_ino) == original_identity


def test_persisted_conflicting_root_is_rejected_without_repair(
    tmp_path: Path, root_registry: GatewayWorkspaceRegistry, bound_target: WorkspaceTarget,
) -> None:
    path = root_registry._storage_path
    state = root_registry._state_store
    duplicate = replace(bound_target, root_path=str(tmp_path / "copy"))
    assert bound_target.root_path is not None
    identity = workspace_identity_path(Path(bound_target.root_path))
    original_identity = identity.read_bytes()
    payload = state.load_workspace_registry() if state is not None else json.loads(path.read_bytes())
    assert payload is not None
    targets = payload["targets"]
    assert isinstance(targets, list)
    targets.append(asdict(duplicate))
    original_database = None
    if state is None:
        path.write_text(json.dumps(payload))
        original = path.read_bytes()
    else:
        # 正式 writer 拒绝重复 owner；仅在损坏场景让新行的列键与重复 payload owner 不一致。
        with closing(state.connection()) as database:
            inserted = database.execute(
                "INSERT INTO gateway_workspace_registry "
                "(gateway_id, workspace_id, position, active, payload_json, updated_at) "
                "SELECT gateway_id, ?, position + 1, 0, ?, updated_at FROM gateway_workspace_registry "
                "WHERE gateway_id = ? AND workspace_id = ?",
                (str(uuid4()), json.dumps(asdict(duplicate)), *bound_target.key),
            )
            assert inserted.rowcount == 1
            database.commit()
            original_database = tuple(database.iterdump())
        original = state.load_workspace_registry()
    for _ in range(2):
        with pytest.raises(WorkspaceIdentityConflictError, match="UUID 已绑定另一物理根") as error:
            GatewayWorkspaceRegistry(storage_path=path, state_store=state, gateway_id=root_registry.gateway_id)
        assert bound_target.workspace_id in str(error.value)
        assert bound_target.root_path in str(error.value)
        assert duplicate.root_path is not None and duplicate.root_path in str(error.value)
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == original
        if state is not None:
            assert original_database is not None
            with closing(state.connection()) as database:
                assert tuple(database.iterdump()) == original_database
    assert identity.read_bytes() == original_identity
    assert root_registry.targets() == (bound_target,)
    assert root_registry.active_workspace == bound_target.key
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
        gateway_id=root_registry.gateway_id, workspace_id=actual, name="新登记", root_path=str(root),
        backend_url="", connection_kind="local",
    ))
    assert root_registry.resolve_local_workspace_identity(Path.cwd(), root, create_directory=True) == actual
    assert (identity.read_bytes(), identity.stat().st_mtime_ns, identity.stat().st_ino) == before
    assert root_registry.resolve(target.key).workspace_id == actual
    assert root_registry.resolve(target.workspace_id).key == (root_registry.gateway_id, actual)
