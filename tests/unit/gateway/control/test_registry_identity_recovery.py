"""通过真实注册、发布和冷重开验证持久身份，禁止恢复时猜测旧字段。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime

LOCAL_GATEWAY_ID = "gateway_" + "a" * 32
REMOTE_GATEWAY_ID = "gateway_" + "b" * 32
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
REMOTE_WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(params=["json", "sqlite"])
def registry_storage(tmp_path: Path, request: pytest.FixtureRequest):
    path = tmp_path / "registry.json"
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite") if request.param == "sqlite" else None
    try:
        yield path, state
    finally:
        if state is not None:
            state.close()


@pytest.fixture
def published_registry(registry_storage):
    path, state = registry_storage
    with closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)) as registry:
        connection = RemoteGatewayConnection(
            connection_id="rgw_identity", name="目标 Gateway", host="remote.example",
            port=22, username="user", private_key_path=None, ssh_config_host="remote",
            remote_gateway_port=8014, remote_gateway_id=REMOTE_GATEWAY_ID,
            protocol_version=FEDERATION_PROTOCOL_VERSION, source_owner="manual",
        )
        projection = WorkspaceTarget(
            gateway_id=REMOTE_GATEWAY_ID, workspace_id=REMOTE_WORKSPACE_ID,
            name="远端工作区", root_path=None,
            backend_url="http://127.0.0.1:43100", connection_kind="remote_gateway",
            owner="remote_projection", target_namespace="gateway-remote",
            remote_gateway_connection_ids=(connection.connection_id,),
            remote_service_names=("workspace_api",),
        )
        registry.apply_remote_projection_snapshot(
            connection=connection, projections=(projection,),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": projection.backend_url}),
        )
        target = registry.resolve(projection.key)
    return path, state, target


@pytest.fixture
def published_local_registry(registry_storage, tmp_path: Path):
    path, state = registry_storage
    with closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)) as registry:
        target = registry.upsert(WorkspaceTarget(
            gateway_id=LOCAL_GATEWAY_ID, workspace_id=WORKSPACE_ID,
            name="本机工作区", root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:43100", connection_kind="local",
            owner="manual",
        ))
    return path, state, target


@pytest.mark.parametrize("field", ["owner", "target_namespace", "remote_gateway_connection_ids", "target_generation"])
def test_cold_reopen_rejects_missing_identity_without_rewriting_record(published_registry, field: str) -> None:
    path, state, _ = published_registry
    payload = state.load_workspace_registry() if state is not None else json.loads(path.read_text())
    del payload["targets"][0][field]
    if state is not None:
        # 模拟外部损坏已提交记录；合法 writer 本身会拒绝部分损坏输入。
        with closing(sqlite3.connect(path.with_name("gateway.sqlite"))) as database:
            database.execute("UPDATE gateway_workspace_registry SET payload_json = ?", (json.dumps(payload["targets"][0]),))
            database.commit()
        before = state.load_workspace_registry()
    else:
        path.write_text(json.dumps(payload))
        before = path.read_bytes()
    for _ in range(2):
        with (
            pytest.raises(ValueError, match=field),
            closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)),
        ):
            pass
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before


@pytest.mark.parametrize(("field", "value", "error_type"), [
    ("owner", "unknown", ValueError), ("owner", None, ValueError),
    ("target_namespace", None, ValueError), ("target_namespace", "", ValueError),
    ("remote_gateway_connection_ids", None, TypeError), ("remote_gateway_connection_ids", "", TypeError),
    ("target_generation", None, ValueError), ("target_generation", "", ValueError),
    ("target_generation", 7, ValueError),
])
def test_cold_reopen_rejects_invalid_identity_without_substitution(
    published_registry, field: str, value: object, error_type: type[Exception],
) -> None:
    path, state, _ = published_registry
    payload = state.load_workspace_registry() if state is not None else json.loads(path.read_text())
    payload["targets"][0][field] = value
    if state is not None:
        with closing(sqlite3.connect(path.with_name("gateway.sqlite"))) as database:
            database.execute("UPDATE gateway_workspace_registry SET payload_json = ?", (json.dumps(payload["targets"][0]),))
            database.commit()
        before = state.load_workspace_registry()
    else:
        path.write_text(json.dumps(payload))
        before = path.read_bytes()
    for _ in range(2):
        with (
            pytest.raises(error_type, match=field),
            closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)),
        ):
            pass
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before


def test_cold_reopen_rejects_missing_local_owner_without_substitution(published_local_registry) -> None:
    path, state, _ = published_local_registry
    payload = state.load_workspace_registry() if state is not None else json.loads(path.read_text())
    del payload["targets"][0]["owner"]
    if state is not None:
        with closing(sqlite3.connect(path.with_name("gateway.sqlite"))) as database:
            database.execute("UPDATE gateway_workspace_registry SET payload_json = ?", (json.dumps(payload["targets"][0]),))
            database.commit()
        before = state.load_workspace_registry()
    else:
        path.write_text(json.dumps(payload))
        before = path.read_bytes()
    for _ in range(2):
        with (
            pytest.raises(ValueError, match="owner"),
            closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)),
        ):
            pass
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before


def test_fresh_publication_reopens_with_exact_identity(published_registry) -> None:
    path, state, target = published_registry
    reopened = GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)
    try:
        assert reopened.resolve(target.key) == target
        assert target.connection_id is None
        assert target.remote_gateway_connection_ids == ("rgw_identity",)
    finally:
        reopened.close()


def test_default_target_owner_cannot_be_reassigned_by_upsert(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    with closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path)) as registry:
        target = registry.upsert(WorkspaceTarget(
            gateway_id=LOCAL_GATEWAY_ID, workspace_id=WORKSPACE_ID,
            name="默认", root_path=str(tmp_path),
            backend_url="http://127.0.0.1:43100", connection_kind="local",
            owner="manual", system_default=True,
        ))
        before = path.read_bytes()
        with pytest.raises(PermissionError, match="owner"):
            registry.upsert(replace(target, owner="system"))
        assert registry.resolve(target.key) == target
        assert path.read_bytes() == before


@pytest.fixture(params=[
    "/private/registry-identity-secret", r"C:\private\registry-identity-secret",
    r"\\server\private\registry-identity-secret",
    "00000000-0000-4000-8000-00000000000A", "00000000000040008000000000000001",
])
def invalid_workspace_identity(request: pytest.FixtureRequest) -> str:
    return request.param


def test_invalid_workspace_identity_upsert_preserves_published_registry(
    published_local_registry, invalid_workspace_identity: str,
) -> None:
    path, state, target = published_local_registry
    with closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)) as registry:
        before = state.load_workspace_registry() if state is not None else path.read_bytes()
        with pytest.raises(RuntimeError, match="标准 UUID") as caught:
            registry.upsert(replace(target, workspace_id=invalid_workspace_identity))
        assert invalid_workspace_identity not in str(caught.value)
        assert "identity-secret" not in str(caught.value)
        assert registry.resolve(target.key) == target
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before


def test_invalid_workspace_identity_cold_reopen_never_echoes_or_repairs_value(
    published_registry, invalid_workspace_identity: str,
) -> None:
    path, state, _ = published_registry
    payload = state.load_workspace_registry() if state is not None else json.loads(path.read_text())
    payload["targets"][0]["workspace_id"] = invalid_workspace_identity
    if state is not None:
        with closing(sqlite3.connect(path.with_name("gateway.sqlite"))) as database:
            database.execute("UPDATE gateway_workspace_registry SET payload_json = ?", (json.dumps(payload["targets"][0]),))
            database.commit()
        before = state.load_workspace_registry()
    else:
        path.write_text(json.dumps(payload))
        before = path.read_bytes()
    for _ in range(2):
        with (
            pytest.raises(RuntimeError, match="标准 UUID") as caught,
            closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)),
        ):
            pass
        assert invalid_workspace_identity not in str(caught.value)
        assert "identity-secret" not in str(caught.value)
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before


def test_invalid_generation_upsert_preserves_published_registry(published_local_registry) -> None:
    path, state, target = published_local_registry
    with closing(GatewayWorkspaceRegistry(gateway_id=LOCAL_GATEWAY_ID, storage_path=path, state_store=state)) as registry:
        before = state.load_workspace_registry() if state is not None else path.read_bytes()
        before_registry = registry._capture_registry_state()
        with pytest.raises(ValueError, match="target_generation"):
            registry.upsert(replace(target, target_generation=7))
        assert registry.resolve(target.key) == target
        assert registry._capture_registry_state() == before_registry
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before
