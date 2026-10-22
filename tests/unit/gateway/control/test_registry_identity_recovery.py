"""通过真实注册、发布和冷重开验证持久身份，禁止恢复时猜测旧字段。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


@pytest.fixture(params=["json", "sqlite"])
def published_registry(tmp_path: Path, request: pytest.FixtureRequest):
    path = tmp_path / "registry.json"
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite") if request.param == "sqlite" else None
    registry = GatewayWorkspaceRegistry(storage_path=path, state_store=state)
    connection = RemoteGatewayConnection(
        connection_id="rgw_identity", name="目标 Gateway", host="remote.example",
        port=22, username="user", private_key_path=None, ssh_config_host="remote",
        remote_gateway_port=8014, remote_gateway_id="remote-gateway",
        protocol_version=1, source_owner="manual",
    )
    registry.upsert_remote_gateway(connection)
    target = registry.upsert(WorkspaceTarget(
        workspace_id="projection", name="远端工作区", root_path=None,
        backend_url="http://127.0.0.1:43100", connection_kind="remote_gateway",
        backend_workspace_id="00000000-0000-4000-8000-000000000002",
        owner="remote_projection", target_namespace="gateway-remote",
        connection_id=connection.connection_id,
        remote_gateway_connection_id=connection.connection_id,
        remote_workspace_id="remote-workspace",
    ))
    registry.close()
    try:
        yield path, state, target
    finally:
        if state is not None:
            state.close()


@pytest.mark.parametrize("field", ["owner", "target_namespace", "connection_id", "target_generation"])
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
        with pytest.raises(ValueError, match=field):
            GatewayWorkspaceRegistry(storage_path=path, state_store=state)
        assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before


@pytest.mark.parametrize(("field", "value"), [
    ("owner", "unknown"), ("owner", None),
    ("target_namespace", None), ("target_namespace", ""),
    ("connection_id", None), ("connection_id", ""),
    ("target_generation", None), ("target_generation", ""),
    ("target_generation", 7),
])
def test_cold_reopen_rejects_invalid_identity_without_substitution(published_registry, field: str, value: object) -> None:
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
    with pytest.raises(ValueError, match=field):
        GatewayWorkspaceRegistry(storage_path=path, state_store=state)
    assert (state.load_workspace_registry() if state is not None else path.read_bytes()) == before


def test_fresh_publication_reopens_with_exact_identity(published_registry) -> None:
    path, state, target = published_registry
    reopened = GatewayWorkspaceRegistry(storage_path=path, state_store=state)
    try:
        assert reopened.resolve(target.workspace_id) == target
    finally:
        reopened.close()


def test_default_target_owner_cannot_be_reassigned_by_upsert(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    registry = GatewayWorkspaceRegistry(storage_path=path)
    target = registry.upsert(WorkspaceTarget(
        workspace_id="default", name="默认", root_path=str(tmp_path),
        backend_url="http://127.0.0.1:43100", connection_kind="local",
        owner="manual", system_default=True,
        backend_workspace_id="00000000-0000-4000-8000-000000000001",
    ))
    before = path.read_bytes()
    with pytest.raises(PermissionError, match="owner"):
        registry.upsert(replace(target, owner="system"))
    assert registry.resolve(target.workspace_id) == target
    assert path.read_bytes() == before
