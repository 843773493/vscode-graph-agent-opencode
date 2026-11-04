"""显式预检只读真实登记，身份冲突不能变成猜测映射或可应用状态。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.workspace_identity import bind_workspace_id, load_or_create_workspace_id
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.user_access import UserAccessContext, UserAccessService
from app.gateway.control.view_state import UserViewStateRecord, UserViewStateStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway_control import WorkspaceFolderCreateRequest
from configs import workspace_identity_migration as migration
from configs.cli import main
from configs.workspace_identity_migration import preview_workspace_identity_migration
from tests.harness.python.run_context import TestRunContext


@dataclass
class Registration:
    root: Path
    workspace: Path
    gateway_id: str
    target: WorkspaceTarget
    store: GatewayStateStore
    registry: GatewayWorkspaceRegistry

    def preview(self):
        return preview_workspace_identity_migration(gateway_root=self.root, project_root=Path.cwd())


@pytest.fixture
def registered(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Registration]:
    root = tmp_path / "home/state/gateway"
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    context = TestRunContext.from_test_file(Path(request.node.path))
    case_root = context.workspace_root / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]
    if case_root.exists():
        shutil.rmtree(case_root)
    workspace = case_root / "original"
    workspace.mkdir(parents=True)
    workspace_id = load_or_create_workspace_id(workspace)
    gateway_id = load_or_create_gateway_id(root / "identity.json")
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        registry = GatewayWorkspaceRegistry(storage_path=root / "workspaces.json", state_store=store)
        target = registry.upsert(WorkspaceTarget(
            workspace_id="gw_old_local", name="不能作为迁移依据的名称", root_path=str(workspace),
            backend_url="http://127.0.0.1:49999", connection_kind="local", backend_workspace_id=workspace_id,
        ))
        try:
            yield Registration(root, workspace, gateway_id, target, store, registry)
        finally:
            registry.close()


def _dump(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return "\n".join(connection.iterdump())


def _view_rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT * FROM user_view_state ORDER BY user_id,workspace_id,session_id"
        )]


@pytest.fixture
def write_view_state(registered: Registration) -> Callable[[str, str, str], UserViewStateRecord]:
    access = UserAccessService(state=registered.store)
    views = UserViewStateStore(state=registered.store)
    contexts: dict[str, UserAccessContext] = {}
    ordinal = 0

    def write(user_id: str, workspace_id: str, session_id: str) -> UserViewStateRecord:
        nonlocal ordinal
        if user_id not in contexts:
            access.create_user(display_name="保留用户显示名", user_id=user_id)
            contexts[user_id] = access.acquire_user(user_id=user_id, client_label="identity-preview")
        ordinal += 1
        return views.put(
            context=contexts[user_id], workspace_id=workspace_id, session_id=session_id,
            turn_anchor=f"保留正文:{registered.target.workspace_id}", scroll_offset=128.5 + ordinal,
            follow_latest=bool(ordinal % 2), projection_version=ordinal,
            tool_details_expanded=not bool(ordinal % 2),
        )

    return write


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr("sys.argv", ["boxteam", "preview-workspace-identity", *args])
    main()


def test_empty_home_preview_never_installs_or_creates_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "missing"
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)

    def reject_write(*_args: object, **_kwargs: object) -> None:
        pytest.fail("只读预检不得创建 store 或安装配置")

    monkeypatch.setattr(GatewayStateStore, "__init__", reject_write)
    monkeypatch.setattr("configs.cli._prepare_storage", reject_write)
    _cli(monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True
    assert report["registry_source"] is report["gateway_id"] is None
    assert report["mappings"] == report["blockers"] == []
    assert report["apply_available"] is False
    assert not home.exists()


def test_existing_registration_is_verified_without_writes(registered: Registration) -> None:
    before = _dump(registered.store.path)
    identity = registered.workspace / ".boxteam/workspace-identity.json"
    original = identity.read_bytes()
    result = registered.preview()
    assert result.blockers == ()
    assert result.report()["mapping_complete"] is True
    mapping, = result.mappings
    assert (mapping.registered_gateway_id, mapping.legacy_workspace_id) == (registered.gateway_id, registered.target.workspace_id)
    assert (mapping.gateway_id, mapping.workspace_id) == (registered.gateway_id, registered.target.backend_workspace_id)
    assert mapping.verified is True
    assert result.registry_revision == registered.registry.registry_revision
    assert result.registry_source == str(registered.store.path)
    assert registered.preview() == result
    assert _dump(registered.store.path) == before
    assert identity.read_bytes() == original
    assert not (registered.workspace / ".boxteam/state").exists()
    assert not (registered.root / "workspaces.json").exists()


def test_same_uuid_in_different_native_roots_blocks_publication(registered: Registration) -> None:
    clone = registered.workspace.with_name(registered.workspace.name + "-clone")
    clone.mkdir(exist_ok=True)
    assert registered.target.backend_workspace_id is not None
    bind_workspace_id(clone, registered.target.backend_workspace_id)
    registered.registry.upsert(WorkspaceTarget(
        workspace_id="gw_clone", name="原目录的副本", root_path=str(clone),
        backend_url="http://127.0.0.1:49998", connection_kind="local",
        backend_workspace_id=registered.target.backend_workspace_id,
    ))
    before = _dump(registered.store.path)
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["duplicate_native_workspace"]
    assert len(result.mappings) == 2
    assert {item.workspace_id for item in result.mappings} == {registered.target.backend_workspace_id}
    assert result.report()["mapping_complete"] is False
    assert _dump(registered.store.path) == before


def test_target_uuid_mismatch_is_reported_and_never_rebound(registered: Registration) -> None:
    actual = registered.target.backend_workspace_id
    assert actual is not None
    registered.target.backend_workspace_id = str(uuid4())
    registered.registry.upsert(registered.target)
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["workspace_identity_mismatch"]
    assert actual in result.blockers[0].detail
    assert result.mappings[0].verified is False
    assert json.loads((registered.workspace / ".boxteam/workspace-identity.json").read_bytes())["workspace_id"] == actual


def test_missing_workspace_identity_is_not_initialized(registered: Registration) -> None:
    identity = registered.workspace / ".boxteam/workspace-identity.json"
    identity.unlink()
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["local_identity_probe_failed"]
    assert result.mappings[0].verified is False
    assert not identity.exists()


def test_missing_cached_uuid_can_only_come_from_existing_native_identity(registered: Registration) -> None:
    actual = registered.target.backend_workspace_id
    registered.target.backend_workspace_id = None
    registered.registry.upsert(registered.target)
    before = _dump(registered.store.path)
    result = registered.preview()
    assert result.blockers == ()
    mapping, = result.mappings
    assert mapping.workspace_id == actual and mapping.verified
    assert _dump(registered.store.path) == before


def test_missing_gateway_identity_is_not_initialized(
    registered: Registration, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    identity = registered.root / "identity.json"
    identity.unlink()
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True
    assert "gateway_identity" in report["error"]
    assert "registry_identity_preflight" in report["diagnostic"]
    assert "FileNotFoundError" in report["diagnostic"]
    assert not identity.exists()


def test_legacy_file_conflicting_with_authoritative_sqlite_is_not_chosen(registered: Registration) -> None:
    payload = registered.store.load_workspace_registry()
    assert payload is not None
    payload["active_workspace_id"] = None
    legacy = registered.root / "workspaces.json"
    legacy.write_text(json.dumps(payload))
    original = legacy.read_bytes()
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["registry_sources_conflict"]
    assert result.registry_source == str(registered.store.path)
    assert legacy.read_bytes() == original


def test_legacy_file_alone_uses_same_mapping_without_creating_sqlite(registered: Registration) -> None:
    payload = registered.store.load_workspace_registry()
    assert payload is not None
    other = registered.root / "file-only"
    other.mkdir()
    (other / "workspaces.json").write_text(json.dumps(payload))
    (other / "identity.json").write_text(json.dumps({"gateway_id": registered.gateway_id}))
    result = preview_workspace_identity_migration(gateway_root=other, project_root=Path.cwd())
    assert result.blockers == ()
    assert result.mappings == registered.preview().mappings
    assert not (other / "gateway.sqlite").exists()


@pytest.mark.parametrize("owner_ids", [
    ("gateway_" + "a" * 32, "gateway_" + "b" * 32),
    ("gateway_" + "a" * 32, "gateway_" + "a" * 32),
])
def test_remote_uuid_keeps_gateway_and_connection_identity_and_requires_confirmation(
    registered: Registration, owner_ids: tuple[str, str],
    write_view_state: Callable[[str, str, str], UserViewStateRecord],
) -> None:
    for index, owner in enumerate(owner_ids):
        connection_id = f"rgw_remote_{index}"
        registered.registry.upsert_remote_gateway(RemoteGatewayConnection(
            connection_id=connection_id, name="名称里不能泄露 secret-sentinel", host="host-sentinel",
            port=22, username="username-sentinel", private_key_path="/credentials/private-sentinel",
            ssh_config_host=None, remote_gateway_port=8014, remote_gateway_id=owner,
            protocol_version=1, source_owner="manual",
        ))
        registered.registry.upsert(WorkspaceTarget(
            workspace_id=f"gw_remote_{index}", name="远程目标", root_path=None,
            backend_url="http://127.0.0.1:49997", connection_kind="remote_gateway",
            owner="remote_projection", connection_id=connection_id, remote_gateway_connection_id=connection_id,
            remote_workspace_id="gw_remote_native", backend_workspace_id=registered.target.backend_workspace_id,
        ))
    navigation_path = registered.root / "navigation/workspace-tree.json"
    WorkspaceNavigationStore(storage_path=navigation_path).list_tree(registered.registry.targets())
    for target in registered.registry.targets():
        write_view_state("remote-user", target.workspace_id, "same-session")
    original_navigation = navigation_path.read_bytes()
    original_database = _dump(registered.store.path)
    original_views = _view_rows(registered.store.path)
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["remote_identity_unverified", "remote_identity_unverified"]
    remote = result.mappings[1:]
    assert [item.gateway_id for item in remote] == list(owner_ids)
    assert {item.workspace_id for item in result.mappings} == {registered.target.backend_workspace_id}
    assert [item.connection_id for item in remote] == ["rgw_remote_0", "rgw_remote_1"]
    assert all(not item.verified for item in remote)
    navigation_refs = [item for item in result.references if item.source == "navigation/workspace-tree.json"]
    view_refs = [item for item in result.references if item.source == "user_view_state.workspace_id"]
    assert [item.gateway_id for item in navigation_refs] == [registered.gateway_id, *owner_ids]
    assert len({item.record_id for item in navigation_refs}) == 3
    assert [(json.loads(item.record_id), item.gateway_id) for item in view_refs] == [
        (["remote-user", identity, "same-session"], owner)
        for identity, owner in zip((registered.target.workspace_id, "gw_remote_0", "gw_remote_1"),
                                   (registered.gateway_id, *owner_ids), strict=True)
    ]
    assert {item.workspace_id for item in result.references} == {registered.target.backend_workspace_id}
    assert navigation_path.read_bytes() == original_navigation
    assert _view_rows(registered.store.path) == original_views
    assert _dump(registered.store.path) == original_database
    assert "sentinel" not in json.dumps(result.report())


def test_live_generation_blocks_preview_and_cli_reports_failure_without_settling(
    registered: Registration, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    registered.store.record_gateway_runtime_generation(
        generation_id="gateway-live", process_id=123, loaded_source="active", candidate_id=None,
        active_revision=1, pending_revision=None, candidate_digest=None, effective_digest="digest",
        secret_binding_digest=None, fencing_token=None, listener_state="serving", state="active",
    )
    before = _dump(registered.store.path)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["blockers"] == [{"code": "unsettled_control_state", "source": "gateway_runtime_generation:gateway-live", "detail": "状态尚未结算: active"}]
    assert report["apply_available"] is False
    assert _dump(registered.store.path) == before


@pytest.mark.parametrize("version", [12, 14])
def test_unknown_registry_version_is_not_guessed(registered: Registration, version: int) -> None:
    registered.store.set_config(config_key="workspace_registry_meta", payload={"schema_version": version}, config_version=1)
    with pytest.raises(ValueError, match="registry 合同 13"):
        registered.preview()


def test_cli_reports_actionable_contract_rejection(
    registered: Registration, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    registered.store.set_config(config_key="workspace_registry_meta", payload={"schema_version": 12}, config_version=1)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "工作区身份预检只支持 registry 合同 13；先完成既有版本升级"
    assert report["read_only"] is True


@pytest.mark.parametrize("database_bytes", [b"", b"not a sqlite database"])
def test_cli_reports_unreadable_database_without_changing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], database_bytes: bytes,
) -> None:
    home = tmp_path / "home"
    database = home / "state/gateway/gateway.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(database_bytes)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True
    assert report["action"] == "preview-workspace-identity"
    assert "registry_identity_preflight" in report["diagnostic"]
    assert report["error"] == (
        "no such table: schema_migrations" if not database_bytes else "file is not a database"
    )
    assert database.read_bytes() == database_bytes
    assert set(database.parent.iterdir()) == {database}


def test_parent_cycle_is_reported_without_recursion_or_repair(registered: Registration) -> None:
    with closing(registered.store.connection()) as connection:
        row = connection.execute("SELECT payload_json FROM gateway_workspace_registry").fetchone()
        payload = json.loads(row[0])
        payload["parent_workspace_id"] = payload["workspace_id"]
        connection.execute("UPDATE gateway_workspace_registry SET payload_json=?", (json.dumps(payload),))
    before = _dump(registered.store.path)
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["workspace_parent_cycle"]
    assert _dump(registered.store.path) == before


def test_registry_metadata_and_rows_share_one_read_snapshot(
    registered: Registration, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = registered.store.load_workspace_registry()
    assert original is not None
    assert isinstance(original["registry_revision"], int)
    factory = registered.store._database.connection
    published = False

    def connection() -> sqlite3.Connection:
        read = factory()

        def during_read(sql: str) -> None:
            nonlocal published
            if "SELECT payload_json FROM gateway_workspace_registry" not in sql or published:
                return
            published = True
            with closing(factory()) as writer:
                writer.execute("BEGIN IMMEDIATE")
                target = json.loads(writer.execute("SELECT payload_json FROM gateway_workspace_registry").fetchone()[0])
                target["name"] = "读取中发布的新版本"
                writer.execute("UPDATE gateway_workspace_registry SET payload_json=?", (json.dumps(target),))
                meta = json.loads(writer.execute("SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'").fetchone()[0])
                meta["registry_revision"] += 1
                writer.execute("UPDATE gateway_config SET payload_json=? WHERE config_key='workspace_registry_meta'", (json.dumps(meta),))
                writer.execute("UPDATE registry_meta SET revision=revision+1 WHERE registry_key='workspace'")
                writer.commit()

        read.set_trace_callback(during_read)
        return read

    monkeypatch.setattr(registered.store._database, "connection", connection)
    assert registered.store.load_workspace_registry() == original
    updated = registered.store.load_workspace_registry()
    assert published and updated is not None
    assert updated["registry_revision"] == original["registry_revision"] + 1
    targets = updated["targets"]
    assert isinstance(targets, list) and isinstance(targets[0], dict)
    assert targets[0]["name"] == "读取中发布的新版本"


def test_navigation_preview_maps_only_workspace_references_without_synchronizing(
    registered: Registration,
) -> None:
    path = registered.root / "navigation/workspace-tree.json"
    navigation = WorkspaceNavigationStore(storage_path=path)
    tree = navigation.create_folder(
        WorkspaceFolderCreateRequest(name=registered.target.workspace_id),
        registered.registry.targets(),
    )
    original = path.read_bytes()
    result = registered.preview()
    assert result.blockers == ()
    node, = [node for node in tree.nodes if node.kind == "workspace_ref"]
    assert result.report()["references"] == [{
        "source": "navigation/workspace-tree.json", "record_id": node.node_id,
        "legacy_workspace_id": registered.target.workspace_id,
        "gateway_id": registered.gateway_id, "workspace_id": registered.target.backend_workspace_id,
    }]
    assert path.read_bytes() == original
    assert any(node.kind == "workspace_folder" and node.name == registered.target.workspace_id for node in tree.nodes)


@pytest.mark.parametrize("corruption", ["unknown_workspace", "duplicate_node", "parent_cycle", "schema_version"])
def test_navigation_corruption_blocks_preview_without_repair(
    registered: Registration, corruption: str,
) -> None:
    path = registered.root / "navigation/workspace-tree.json"
    navigation = WorkspaceNavigationStore(storage_path=path)
    navigation.create_folder(WorkspaceFolderCreateRequest(name="保留原目录"), registered.registry.targets())
    payload = json.loads(path.read_bytes())
    if corruption == "unknown_workspace":
        node, = [node for node in payload["nodes"] if node["kind"] == "workspace_ref"]
        node["workspace_id"] = "gw_unmapped"
    elif corruption == "duplicate_node":
        payload["nodes"].append(payload["nodes"][0])
    elif corruption == "parent_cycle":
        node, = [node for node in payload["nodes"] if node["kind"] == "workspace_folder"]
        node["parent_node_id"] = node["node_id"]
    else:
        payload["schema_version"] = 2
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    result = registered.preview()
    blocker, = result.blockers
    assert blocker.code == "invalid_navigation_reference"
    assert blocker.source == str(path)
    assert result.report()["mapping_complete"] is False
    assert result.references == ()
    assert path.read_bytes() == original


def test_navigation_without_registry_is_not_discarded_or_given_new_identity(tmp_path: Path) -> None:
    path = tmp_path / "navigation/workspace-tree.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"schema_version": 1, "nodes": [{
        "kind": "workspace_ref", "node_id": "retained-node", "workspace_id": "gw_orphan", "name": "保留引用",
    }]}))
    original = path.read_bytes()
    result = preview_workspace_identity_migration(gateway_root=tmp_path, project_root=Path.cwd())
    blocker, = result.blockers
    assert blocker.code == "invalid_navigation_reference"
    assert result.mappings == ()
    assert result.report()["mapping_complete"] is False
    assert path.read_bytes() == original
    assert not (tmp_path / "identity.json").exists()
    assert not (tmp_path / "gateway.sqlite").exists()


def test_user_view_preview_maps_complete_original_keys_and_keeps_every_view_field(
    registered: Registration, write_view_state: Callable[[str, str, str], UserViewStateRecord],
) -> None:
    other_root = registered.workspace.with_name("other")
    other_root.mkdir()
    other_id = load_or_create_workspace_id(other_root)
    other = registered.registry.upsert(WorkspaceTarget(
        workspace_id=other_id, name=registered.target.workspace_id, root_path=str(other_root),
        backend_url="http://127.0.0.1:49998", connection_kind="local", backend_workspace_id=other_id,
    ))
    registered.registry.activate(other.workspace_id)
    records = [
        write_view_state("user-a", registered.target.workspace_id, "same-session"),
        write_view_state("user-a", registered.target.workspace_id, "session:with:separators"),
        write_view_state("user-b", registered.target.workspace_id, "same-session"),
        write_view_state("user-b", other.workspace_id, "same-session"),
    ]
    records.sort(key=lambda row: (row.user_id, row.workspace_id, row.session_id))
    before = _dump(registered.store.path)
    original_views = _view_rows(registered.store.path)
    assert original_views == [astuple(record) for record in records]
    result = registered.preview()
    assert result.blockers == () and result.report()["mapping_complete"] is True
    assert result.report()["references"] == [{
        "source": "user_view_state.workspace_id",
        "record_id": json.dumps((record.user_id, record.workspace_id, record.session_id), separators=(",", ":")),
        "legacy_workspace_id": record.workspace_id, "gateway_id": registered.gateway_id,
        "workspace_id": other_id if record.workspace_id == other_id else registered.target.backend_workspace_id,
    } for record in records]
    assert _view_rows(registered.store.path) == original_views
    assert _dump(registered.store.path) == before
    assert registered.preview() == result


@pytest.mark.parametrize("reference_kind", ["unknown_key", "display_name", "backend_uuid"])
def test_user_view_unknown_reference_blocks_without_inferring_owner_or_changing_rows(
    registered: Registration, write_view_state: Callable[[str, str, str], UserViewStateRecord],
    reference_kind: str,
) -> None:
    assert registered.target.backend_workspace_id is not None
    workspace_id = {
        "unknown_key": "gw_unregistered", "display_name": registered.target.name,
        "backend_uuid": registered.target.backend_workspace_id,
    }[reference_kind]
    write_view_state("user-a", registered.target.workspace_id, "known-session")
    unknown = write_view_state("user-a", workspace_id, registered.target.workspace_id)
    before, original_views = _dump(registered.store.path), _view_rows(registered.store.path)
    result = registered.preview()
    blocker, = result.blockers
    record_id = json.dumps((unknown.user_id, unknown.workspace_id, unknown.session_id),
                           ensure_ascii=False, separators=(",", ":"))
    assert blocker.code == "unknown_user_view_workspace_reference"
    assert blocker.source == f"user_view_state:{record_id}" and workspace_id in blocker.detail
    assert result.report()["mapping_complete"] is False and result.report()["apply_available"] is False
    reference, = result.references
    assert reference.legacy_workspace_id == registered.target.workspace_id
    assert _view_rows(registered.store.path) == original_views
    assert _dump(registered.store.path) == before


def test_user_view_without_registry_preserves_orphan_and_does_not_create_identity(
    registered: Registration, write_view_state: Callable[[str, str, str], UserViewStateRecord],
) -> None:
    write_view_state("user-a", registered.target.workspace_id, "orphan-session")
    with closing(registered.store.connection()) as connection:
        connection.execute("DELETE FROM gateway_workspace_registry")
        connection.execute("DELETE FROM gateway_config WHERE config_key='workspace_registry_meta'")
        connection.execute("DELETE FROM registry_meta WHERE registry_key='workspace'")
    identity = registered.root / "identity.json"
    identity.unlink()
    before, original_views = _dump(registered.store.path), _view_rows(registered.store.path)
    result = registered.preview()
    blocker, = result.blockers
    assert blocker.code == "unknown_user_view_workspace_reference"
    assert result.mappings == result.references == () and result.gateway_id is None
    assert result.report()["mapping_complete"] is False
    assert _view_rows(registered.store.path) == original_views and _dump(registered.store.path) == before
    assert not identity.exists()


def test_user_view_ambiguous_registered_key_is_rejected_without_changing_original_rows(
    registered: Registration, write_view_state: Callable[[str, str, str], UserViewStateRecord],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    write_view_state("user-a", registered.target.workspace_id, "retained-session")
    with closing(registered.store.connection()) as connection:
        row = connection.execute("SELECT payload_json,updated_at FROM gateway_workspace_registry").fetchone()
        payload = json.loads(row[0])
        payload["backend_workspace_id"] = str(uuid4())
        connection.execute(
            "INSERT INTO gateway_workspace_registry(workspace_id,position,active,payload_json,updated_at) VALUES(?,?,?,?,?)",
            ("gw_duplicate_row", 1, 0, json.dumps(payload), row[1]),
        )
    before, original_views = _dump(registered.store.path), _view_rows(registered.store.path)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True and report["error"] == "Gateway registry 工作区别名重复"
    assert _view_rows(registered.store.path) == original_views and _dump(registered.store.path) == before


def test_user_view_and_registry_are_read_from_one_snapshot(
    registered: Registration, write_view_state: Callable[[str, str, str], UserViewStateRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_key = registered.target.workspace_id
    original = write_view_state("user-a", original_key, "snapshot-session")
    factory = sqlite3.connect
    read_connections: list[str] = []
    published_dump: str | None = None
    replacement_key = "gw_after_snapshot"

    def during_read(sql: str) -> None:
        nonlocal published_dump
        if "FROM user_view_state" not in sql or published_dump is not None:
            return
        with closing(factory(registered.store.path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            target = json.loads(writer.execute("SELECT payload_json FROM gateway_workspace_registry").fetchone()[0])
            target["workspace_id"] = replacement_key
            writer.execute("UPDATE gateway_workspace_registry SET workspace_id=?,payload_json=?",
                           (replacement_key, json.dumps(target)))
            metadata = json.loads(writer.execute(
                "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'"
            ).fetchone()[0])
            metadata["registry_revision"] += 1
            metadata["active_workspace_id"] = replacement_key
            writer.execute("UPDATE gateway_config SET payload_json=? WHERE config_key='workspace_registry_meta'",
                           (json.dumps(metadata),))
            writer.execute("UPDATE registry_meta SET revision=revision+1 WHERE registry_key='workspace'")
            writer.execute("UPDATE user_view_state SET workspace_id=?,turn_anchor=?",
                           (replacement_key, "并发 writer 提交的新正文"))
            writer.commit()
            published_dump = "\n".join(writer.iterdump())

    def connect(database: str, *, uri: bool) -> sqlite3.Connection:
        assert uri and database.endswith("?mode=ro")
        read_connections.append(database)
        connection = factory(database, uri=uri)
        connection.set_trace_callback(during_read)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(migration.sqlite3, "connect", connect)
        result = registered.preview()
    assert len(read_connections) == 1 and published_dump is not None
    assert result.blockers == ()
    mapping, = result.mappings
    reference, = result.references
    assert mapping.legacy_workspace_id == reference.legacy_workspace_id == original_key
    assert json.loads(reference.record_id) == [original.user_id, original.workspace_id, original.session_id]
    assert _dump(registered.store.path) == published_dump
    assert _view_rows(registered.store.path)[0][1:4] == (
        replacement_key, original.session_id, "并发 writer 提交的新正文",
    )
