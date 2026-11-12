"""显式预检只读真实登记，身份冲突不能变成猜测映射或可应用状态。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.gateway.control import generator_import as generator_import_owner
from app.gateway.control.gateway_registry.snapshot import read_workspace_registry
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.generator_import import import_session_generators
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from configs.cli import main
from configs.maintenance import workspace_identity_migration as migration
from configs.maintenance.workspace_identity_migration import (
    preview_workspace_identity_migration,
)
from tests.unit.configs.conftest import LegacyGateway

LOCAL_ALIAS = "gw_local_0"


def _workspace_id(case: LegacyGateway, alias: str = LOCAL_ALIAS) -> str:
    return case.metadata["workspaces"][alias]["workspace_id"]


def _registry(case: LegacyGateway) -> dict[str, object]:
    with closing(sqlite3.connect(case.database)) as connection:
        payload = read_workspace_registry(connection)
    assert payload is not None
    return payload


def _alter_target(case: LegacyGateway, alias: str, **changes: object) -> None:
    # 只离线改变冻结输入副本，不调用新 runtime 重新生成旧登记。
    with closing(sqlite3.connect(case.database)) as connection:
        payload = json.loads(connection.execute(
            "SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (alias,),
        ).fetchone()[0])
        connection.execute("UPDATE gateway_workspace_registry SET payload_json=? WHERE workspace_id=?",
                           (json.dumps({**payload, **changes}), alias))
        connection.commit()


def _alter_metadata(case: LegacyGateway, **changes: object) -> None:
    with closing(sqlite3.connect(case.database)) as connection:
        payload = json.loads(connection.execute(
            "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'",
        ).fetchone()[0])
        connection.execute("UPDATE gateway_config SET payload_json=? WHERE config_key='workspace_registry_meta'",
                           (json.dumps({**payload, **changes}),))
        connection.commit()


@pytest.fixture
def registered(legacy_gateway: LegacyGateway) -> LegacyGateway:
    with closing(sqlite3.connect(legacy_gateway.database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    return legacy_gateway


@pytest.fixture
def current_state(registered: LegacyGateway) -> Iterator[GatewayStateStore]:
    # 当前 owner 生成业务状态，旧库只接收离线复制的旧格式输入。
    with GatewayStateStore(path=registered.directory / "current-owner/gateway.sqlite") as state:
        yield state


def _dump(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return "\n".join(connection.iterdump())


def _view_rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT * FROM user_view_state ORDER BY user_id,workspace_id,session_id"
        )]


@pytest.fixture
def write_view_state(registered: LegacyGateway, current_state: GatewayStateStore) -> Callable[[str, str, str], sqlite3.Row]:
    ordinal = 0
    with closing(sqlite3.connect(registered.database)) as connection:
        connection.execute("DELETE FROM user_view_state")
        connection.commit()

    def write(user_id: str, workspace_id: str, session_id: str) -> sqlite3.Row:
        nonlocal ordinal
        ordinal += 1
        with closing(sqlite3.connect(registered.database)) as destination:
            destination.execute("INSERT OR IGNORE INTO user_account VALUES(?,?,?,?)",
                (user_id, "保留用户显示名", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            destination.execute("INSERT INTO user_view_state VALUES(?,?,?,?,?,?,?,?,?)",
                (user_id, workspace_id, session_id, f"保留正文:{LOCAL_ALIAS}", 128.5 + ordinal,
                 bool(ordinal % 2), ordinal, not bool(ordinal % 2), "2026-01-01T00:00:00Z"))
            destination.commit()
            destination.row_factory = sqlite3.Row
            return destination.execute("SELECT * FROM user_view_state WHERE user_id=? AND workspace_id=? AND session_id=?",
                                       (user_id, workspace_id, session_id)).fetchone()

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


def test_existing_registration_is_verified_without_writes(registered: LegacyGateway) -> None:
    before = _dump(registered.database)
    identity = (registered.directory / "workspace-0") / ".boxteam/workspace-identity.json"
    original = identity.read_bytes()
    result = registered.preview()
    assert result.blockers == ()
    assert result.report()["mapping_complete"] is result.report()["apply_available"] is True
    assert len(result.mappings) == 3
    mapping = result.mappings[0]
    assert (mapping.registered_gateway_id, mapping.legacy_workspace_id) == (registered.gateway_id, LOCAL_ALIAS)
    assert (mapping.gateway_id, mapping.workspace_id) == (registered.gateway_id, _workspace_id(registered))
    assert mapping.verified is True
    assert result.registry_revision == _registry(registered)["registry_revision"]
    assert result.registry_source == str(registered.database)
    assert registered.preview() == result
    assert _dump(registered.database) == before
    assert identity.read_bytes() == original
    assert not ((registered.directory / "workspace-0") / ".boxteam/state").exists()
    assert not (registered.root / "workspaces.json").exists()


def test_same_uuid_in_different_native_roots_blocks_publication(registered: LegacyGateway) -> None:
    clone = registered.directory / "workspace-1"
    identity = (registered.directory / "workspace-0") / ".boxteam/workspace-identity.json"
    clone_identity = clone / ".boxteam/workspace-identity.json"
    shutil.copyfile(identity, clone_identity)
    _alter_target(registered, "gw_local_1", backend_workspace_id=_workspace_id(registered))
    backends = _registry(registered)["managed_backends"]
    assert isinstance(backends, list)
    for backend in backends:
        for member in backend["members"]:
            if member["workspace_id"] == "gw_local_1":
                member["backend_workspace_id"] = _workspace_id(registered)
    _alter_metadata(registered, managed_backends=backends)
    before = _dump(registered.database)
    navigation = registered.root / "navigation/workspace-tree.json"
    original_navigation = navigation.read_bytes()
    original_identity = identity.read_bytes()
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["duplicate_native_workspace", "invalid_navigation_reference"]
    assert len(result.mappings) == 3
    assert [item.workspace_id for item in result.mappings[:2]] == [_workspace_id(registered)] * 2
    assert result.report()["mapping_complete"] is False
    assert _dump(registered.database) == before
    assert navigation.read_bytes() == original_navigation
    assert identity.read_bytes() == clone_identity.read_bytes() == original_identity


def test_target_uuid_mismatch_is_reported_and_never_rebound(registered: LegacyGateway) -> None:
    actual = _workspace_id(registered)
    _alter_target(registered, LOCAL_ALIAS, backend_workspace_id=str(uuid4()))
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["workspace_identity_mismatch"]
    assert actual in result.blockers[0].detail
    assert result.mappings[0].verified is False
    assert json.loads(((registered.directory / "workspace-0") / ".boxteam/workspace-identity.json").read_bytes())["workspace_id"] == actual


def test_missing_workspace_identity_is_not_initialized(registered: LegacyGateway) -> None:
    identity = (registered.directory / "workspace-0") / ".boxteam/workspace-identity.json"
    identity.unlink()
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["local_identity_probe_failed"]
    assert result.mappings[0].verified is False
    assert not identity.exists()


def test_missing_cached_uuid_can_only_come_from_existing_native_identity(registered: LegacyGateway) -> None:
    actual = _workspace_id(registered)
    _alter_target(registered, LOCAL_ALIAS, backend_workspace_id=None)
    before = _dump(registered.database)
    result = registered.preview()
    assert result.blockers == ()
    mapping = result.mappings[0]
    assert mapping.workspace_id == actual and mapping.verified
    assert _dump(registered.database) == before


def test_missing_gateway_identity_is_not_initialized(
    registered: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
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


def test_legacy_file_conflicting_with_authoritative_sqlite_is_not_chosen(registered: LegacyGateway) -> None:
    payload = _registry(registered)
    payload["active_workspace_id"] = None
    legacy = registered.root / "workspaces.json"
    legacy.write_text(json.dumps(payload))
    original = legacy.read_bytes()
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["registry_sources_conflict"]
    assert result.registry_source == str(registered.database)
    assert legacy.read_bytes() == original


def test_legacy_file_alone_uses_same_mapping_without_creating_sqlite(registered: LegacyGateway) -> None:
    payload = _registry(registered)
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
@pytest.mark.parametrize("legacy_gateway", [True], indirect=True)
def test_remote_uuid_keeps_gateway_and_connection_identity_and_requires_confirmation(
    registered: LegacyGateway, owner_ids: tuple[str, str],
    write_view_state: Callable[[str, str, str], sqlite3.Row],
) -> None:
    connections = _registry(registered)["remote_gateway_connections"]
    assert isinstance(connections, list)
    for index, owner in enumerate(owner_ids):
        connections[index].update(remote_gateway_id=owner, name="名称里不能泄露 secret-sentinel", host="host-sentinel",
                                  username="username-sentinel", private_key_path="/credentials/private-sentinel")
        _alter_target(registered, f"gw_remote_{index}", backend_workspace_id=_workspace_id(registered))
    _alter_metadata(registered, remote_gateway_connections=connections)
    navigation_path = registered.root / "navigation/workspace-tree.json"
    navigation = json.loads(navigation_path.read_bytes())
    distinct_owners = list(dict.fromkeys(owner_ids))
    navigation["nodes"].extend({**navigation["nodes"][0], "node_id": f"gwn_remote_{index}",
                                "workspace_id": f"gw_remote_{owner_ids.index(owner)}", "position": 4 + index}
                               for index, owner in enumerate(distinct_owners))
    navigation_path.write_text(json.dumps(navigation))
    aliases = ["gw_local_0", "gw_local_1", "gw_local_2", "gw_remote_0", "gw_remote_1"]
    for alias in aliases:
        write_view_state("remote-user", alias, "same-session")
    original_navigation = navigation_path.read_bytes()
    original_database = _dump(registered.database)
    original_views = _view_rows(registered.database)
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["remote_identity_unverified", "remote_identity_unverified"]
    remote = result.mappings[3:]
    assert [item.gateway_id for item in remote] == list(owner_ids)
    assert {item.workspace_id for item in remote} == {_workspace_id(registered)}
    assert [item.connection_id for item in remote] == ["rgw_fixture_0", "rgw_fixture_1"]
    assert all(not item.verified for item in remote)
    navigation_refs = [item for item in result.references if item.source == "navigation/workspace-tree.json"]
    view_refs = [item for item in result.references if item.source == "user_view_state.workspace_id"]
    assert [item.gateway_id for item in navigation_refs] == [registered.gateway_id] * 3 + distinct_owners
    assert len({item.record_id for item in navigation_refs}) == 3 + len(distinct_owners)
    assert [(json.loads(item.record_id), item.gateway_id) for item in view_refs] == [
        (["remote-user", identity, "same-session"], owner)
        for identity, owner in zip(aliases, [registered.gateway_id] * 3 + list(owner_ids), strict=True)
    ]
    assert {item.workspace_id for item in result.references} == {value["workspace_id"] for value in registered.metadata["workspaces"].values()}
    assert navigation_path.read_bytes() == original_navigation
    assert _view_rows(registered.database) == original_views
    assert _dump(registered.database) == original_database
    assert "sentinel" not in json.dumps(result.report())


@pytest.mark.parametrize("legacy_gateway", [True], indirect=True)
def test_navigation_aliases_converging_on_one_native_owner_block_without_repair(registered: LegacyGateway) -> None:
    path = registered.root / "navigation/workspace-tree.json"
    navigation = json.loads(path.read_bytes())
    navigation["nodes"].extend({**navigation["nodes"][0], "node_id": f"gwn_remote_{index}",
                                "workspace_id": f"gw_remote_{index}", "position": 4 + index} for index in range(2))
    path.write_text(json.dumps(navigation))
    before, original = _dump(registered.database), path.read_bytes()
    result = registered.preview()
    assert [item.code for item in result.blockers] == [
        "remote_identity_unverified", "remote_identity_unverified", "invalid_navigation_reference",
    ]
    assert result.blockers[-1].source == str(path)
    assert "重复规范引用" in result.blockers[-1].detail
    assert result.report()["mapping_complete"] is result.report()["apply_available"] is False
    assert not [item for item in result.references if item.source == "navigation/workspace-tree.json"]
    assert path.read_bytes() == original and _dump(registered.database) == before


def test_live_generation_blocks_preview_and_cli_reports_failure_without_settling(
    registered: LegacyGateway, current_state: GatewayStateStore,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    current_state.record_gateway_runtime_generation(
        generation_id="gateway-live", process_id=123, loaded_source="active", candidate_id=None,
        active_revision=1, pending_revision=None, candidate_digest=None, effective_digest="digest",
        secret_binding_digest=None, fencing_token=None, listener_state="serving", state="active",
    )
    with closing(current_state.connection()) as source, closing(sqlite3.connect(registered.database)) as destination:
        row = source.execute("SELECT * FROM gateway_runtime_generation WHERE generation_id='gateway-live'").fetchone()
        destination.execute(f"INSERT INTO gateway_runtime_generation VALUES({','.join('?' for _ in row)})", tuple(row))
        destination.commit()
    before = _dump(registered.database)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["blockers"] == [{"code": "unsettled_control_state", "source": "gateway_runtime_generation:gateway-live", "detail": "状态尚未结算: active"}]
    assert report["apply_available"] is False
    assert _dump(registered.database) == before


@pytest.mark.parametrize("version", [12, 14])
def test_unknown_registry_version_is_not_guessed(registered: LegacyGateway, version: int) -> None:
    _alter_metadata(registered, schema_version=version)
    with pytest.raises(ValueError, match="registry 合同 13"):
        registered.preview()


def test_cli_reports_actionable_contract_rejection(
    registered: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _alter_metadata(registered, schema_version=12)
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


def test_parent_cycle_is_reported_without_recursion_or_repair(registered: LegacyGateway) -> None:
    _alter_target(registered, LOCAL_ALIAS, parent_workspace_id=LOCAL_ALIAS)
    before = _dump(registered.database)
    result = registered.preview()
    assert [item.code for item in result.blockers] == ["workspace_parent_cycle"]
    assert _dump(registered.database) == before


def test_registry_metadata_and_rows_share_one_read_snapshot(
    registered: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = registered.preview()
    assert original.registry_revision is not None
    factory = sqlite3.connect
    published = False

    def connection(database: str, *, uri: bool) -> sqlite3.Connection:
        assert uri and database.endswith("?mode=ro")
        read = factory(database, uri=uri)

        def during_read(sql: str) -> None:
            nonlocal published
            if "SELECT payload_json FROM gateway_workspace_registry" not in sql or published:
                return
            published = True
            with closing(factory(registered.database)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                target = json.loads(writer.execute("SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (LOCAL_ALIAS,)).fetchone()[0])
                target["name"] = "读取中发布的新版本"
                writer.execute("UPDATE gateway_workspace_registry SET payload_json=? WHERE workspace_id=?", (json.dumps(target), LOCAL_ALIAS))
                meta = json.loads(writer.execute("SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'").fetchone()[0])
                meta["registry_revision"] += 1
                writer.execute("UPDATE gateway_config SET payload_json=? WHERE config_key='workspace_registry_meta'", (json.dumps(meta),))
                writer.execute("UPDATE registry_meta SET revision=revision+1 WHERE registry_key='workspace'")
                writer.commit()

        read.set_trace_callback(during_read)
        return read

    with monkeypatch.context() as patch:
        patch.setattr(migration.sqlite3, "connect", connection)
        assert registered.preview() == original
        updated = registered.preview()
    assert published and updated.blockers == ()
    assert updated.registry_revision == original.registry_revision + 1
    assert updated.registry_digest != original.registry_digest
    targets = _registry(registered)["targets"]
    assert isinstance(targets, list) and isinstance(targets[0], dict)
    assert targets[0]["name"] == "读取中发布的新版本"


def test_navigation_preview_maps_only_workspace_references_without_synchronizing(
    registered: LegacyGateway,
) -> None:
    path = registered.root / "navigation/workspace-tree.json"
    tree = json.loads(path.read_bytes())
    folder, = [node for node in tree["nodes"] if node["kind"] == "workspace_folder"]
    folder["name"] = LOCAL_ALIAS
    path.write_text(json.dumps(tree))
    original = path.read_bytes()
    result = registered.preview()
    assert result.blockers == ()
    references = result.report()["references"]
    assert isinstance(references, list)
    assert [item for item in references if item["source"] == "navigation/workspace-tree.json"] == [{
        "source": "navigation/workspace-tree.json", "record_id": node["node_id"],
        "legacy_workspace_id": node["workspace_id"],
        "gateway_id": registered.gateway_id, "workspace_id": _workspace_id(registered, node["workspace_id"]),
    } for node in tree["nodes"] if node["kind"] == "workspace_ref"]
    assert path.read_bytes() == original
    assert folder["kind"] == "workspace_folder" and folder["name"] == LOCAL_ALIAS


@pytest.mark.parametrize("corruption", ["unknown_workspace", "duplicate_node", "parent_cycle", "schema_version"])
def test_navigation_corruption_blocks_preview_without_repair(
    registered: LegacyGateway, corruption: str,
) -> None:
    path = registered.root / "navigation/workspace-tree.json"
    payload = json.loads(path.read_bytes())
    if corruption == "unknown_workspace":
        node = next(node for node in payload["nodes"] if node["kind"] == "workspace_ref")
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
    assert not [item for item in result.references if item.source == "navigation/workspace-tree.json"]
    assert path.read_bytes() == original


def test_navigation_without_registry_is_not_discarded_or_given_new_identity(registered: LegacyGateway) -> None:
    path = registered.root / "navigation/workspace-tree.json"
    payload = json.loads(path.read_bytes())
    payload["nodes"][0]["workspace_id"] = "gw_orphan"
    path.write_text(json.dumps(payload))
    registered.database.unlink()
    (registered.root / "identity.json").unlink()
    original = path.read_bytes()
    result = registered.preview()
    blocker, = result.blockers
    assert blocker.code == "invalid_navigation_reference"
    assert result.mappings == ()
    assert result.report()["mapping_complete"] is False
    assert path.read_bytes() == original
    assert not (registered.root / "identity.json").exists()
    assert not registered.database.exists()


def test_user_view_preview_maps_complete_original_keys_and_keeps_every_view_field(
    registered: LegacyGateway, write_view_state: Callable[[str, str, str], sqlite3.Row],
) -> None:
    other_alias = "gw_local_1"
    _alter_target(registered, other_alias, name=LOCAL_ALIAS)
    _alter_metadata(registered, active_workspace_id=other_alias)
    with closing(sqlite3.connect(registered.database)) as connection:
        connection.execute("UPDATE gateway_workspace_registry SET active=(workspace_id=?)", (other_alias,))
        connection.commit()
    records = [
        write_view_state("user-a", LOCAL_ALIAS, "same-session"),
        write_view_state("user-a", LOCAL_ALIAS, "session:with:separators"),
        write_view_state("user-b", LOCAL_ALIAS, "same-session"),
        write_view_state("user-b", other_alias, "same-session"),
    ]
    records.sort(key=lambda row: (row["user_id"], row["workspace_id"], row["session_id"]))
    before = _dump(registered.database)
    original_views = _view_rows(registered.database)
    assert original_views == [tuple(record) for record in records]
    result = registered.preview()
    assert result.blockers == () and result.report()["mapping_complete"] is True
    references = result.report()["references"]
    assert isinstance(references, list)
    assert [item for item in references if item["source"] == "user_view_state.workspace_id"] == [{
        "source": "user_view_state.workspace_id",
        "record_id": json.dumps((record["user_id"], record["workspace_id"], record["session_id"]), separators=(",", ":")),
        "legacy_workspace_id": record["workspace_id"], "gateway_id": registered.gateway_id,
        "workspace_id": _workspace_id(registered, record["workspace_id"]),
    } for record in records]
    assert _view_rows(registered.database) == original_views
    assert _dump(registered.database) == before
    assert registered.preview() == result


@pytest.mark.parametrize("reference_kind", ["unknown_key", "display_name", "backend_uuid"])
def test_user_view_unknown_reference_blocks_without_inferring_owner_or_changing_rows(
    registered: LegacyGateway, write_view_state: Callable[[str, str, str], sqlite3.Row],
    reference_kind: str,
) -> None:
    assert _workspace_id(registered) is not None
    workspace_id = {
        "unknown_key": "gw_unregistered", "display_name": "工作区 0",
        "backend_uuid": _workspace_id(registered),
    }[reference_kind]
    write_view_state("user-a", LOCAL_ALIAS, "known-session")
    unknown = write_view_state("user-a", workspace_id, LOCAL_ALIAS)
    before, original_views = _dump(registered.database), _view_rows(registered.database)
    result = registered.preview()
    blocker, = result.blockers
    record_id = json.dumps((unknown["user_id"], unknown["workspace_id"], unknown["session_id"]),
                           ensure_ascii=False, separators=(",", ":"))
    assert blocker.code == "unknown_user_view_workspace_reference"
    assert blocker.source == f"user_view_state:{record_id}" and workspace_id in blocker.detail
    assert result.report()["mapping_complete"] is False and result.report()["apply_available"] is False
    reference, = [item for item in result.references if item.source == "user_view_state.workspace_id"]
    assert reference.legacy_workspace_id == LOCAL_ALIAS
    assert _view_rows(registered.database) == original_views
    assert _dump(registered.database) == before


def test_user_view_without_registry_preserves_orphan_and_does_not_create_identity(
    registered: LegacyGateway, write_view_state: Callable[[str, str, str], sqlite3.Row],
) -> None:
    write_view_state("user-a", LOCAL_ALIAS, "orphan-session")
    with closing(sqlite3.connect(registered.database)) as connection:
        connection.execute("DELETE FROM gateway_workspace_registry")
        connection.execute("DELETE FROM gateway_config WHERE config_key='workspace_registry_meta'")
        connection.execute("DELETE FROM registry_meta WHERE registry_key='workspace'")
        connection.commit()
    # 本用例只检查视图孤项，导航孤项由独立用例保留并验证。
    (registered.root / "navigation/workspace-tree.json").unlink()
    identity = registered.root / "identity.json"
    identity.unlink()
    before, original_views = _dump(registered.database), _view_rows(registered.database)
    result = registered.preview()
    blocker, = result.blockers
    assert blocker.code == "unknown_user_view_workspace_reference"
    assert result.mappings == result.references == () and result.gateway_id is None
    assert result.report()["mapping_complete"] is False
    assert _view_rows(registered.database) == original_views and _dump(registered.database) == before
    assert not identity.exists()


def test_user_view_ambiguous_registered_key_is_rejected_without_changing_original_rows(
    registered: LegacyGateway, write_view_state: Callable[[str, str, str], sqlite3.Row],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    write_view_state("user-a", LOCAL_ALIAS, "retained-session")
    with closing(sqlite3.connect(registered.database)) as connection:
        row = connection.execute("SELECT payload_json,updated_at FROM gateway_workspace_registry").fetchone()
        payload = json.loads(row[0])
        payload["backend_workspace_id"] = str(uuid4())
        connection.execute(
            "INSERT INTO gateway_workspace_registry(workspace_id,position,active,payload_json,updated_at) VALUES(?,?,?,?,?)",
            ("gw_duplicate_row", 1, 0, json.dumps(payload), row[1]),
        )
        connection.commit()
    before, original_views = _dump(registered.database), _view_rows(registered.database)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["read_only"] is True and report["error"] == "Gateway registry 工作区别名重复"
    assert _view_rows(registered.database) == original_views and _dump(registered.database) == before


def test_user_view_and_registry_are_read_from_one_snapshot(
    registered: LegacyGateway, write_view_state: Callable[[str, str, str], sqlite3.Row],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_key = LOCAL_ALIAS
    original = write_view_state("user-a", original_key, "snapshot-session")
    factory = sqlite3.connect
    read_connections: list[str] = []
    published_dump: str | None = None
    replacement_key = "gw_after_snapshot"

    def during_read(sql: str) -> None:
        nonlocal published_dump
        if "FROM user_view_state" not in sql or published_dump is not None:
            return
        with closing(factory(registered.database)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            target = json.loads(writer.execute("SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (original_key,)).fetchone()[0])
            target["workspace_id"] = replacement_key
            writer.execute("UPDATE gateway_workspace_registry SET workspace_id=?,payload_json=? WHERE workspace_id=?",
                           (replacement_key, json.dumps(target), original_key))
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
    mapping = result.mappings[0]
    reference, = [item for item in result.references if item.source == "user_view_state.workspace_id"]
    assert mapping.legacy_workspace_id == reference.legacy_workspace_id == original_key
    assert json.loads(reference.record_id) == [original["user_id"], original["workspace_id"], original["session_id"]]
    assert _dump(registered.database) == published_dump
    assert _view_rows(registered.database)[0][1:4] == (
        replacement_key, original["session_id"], "并发 writer 提交的新正文",
    )


@dataclass
class GeneratorImportCase:
    registered: LegacyGateway
    registry: GatewayWorkspaceRegistry
    client: httpx.AsyncClient
    api: GatewayResourceCatalogService
    sent: list[httpx.Request]

    @property
    def workspace_id(self) -> str:
        return _workspace_id(self.registered)

    async def run(self, *, preview: bool = False) -> list[dict[str, str]]:
        return await import_session_generators(
            root=self.registered.root, registry=self.registry, api=self.api,
            http_client=self.client, gateway_id=self.registered.gateway_id,
            request_id="req_identity_generator_preflight", preview=preview,
        )

    @property
    def backup(self) -> Path:
        snapshot, = (self.registered.root / "migration-backups/session-generators").glob("*/snapshot.json")
        return snapshot.parent


@pytest.fixture
async def generator_case(registered: LegacyGateway, current_state: GatewayStateStore) -> AsyncIterator[GeneratorImportCase]:
    root, workspace_id = registered.root, _workspace_id(registered)
    registry = GatewayWorkspaceRegistry(storage_path=current_state.path.parent / "workspaces.json",
                                        state_store=current_state, gateway_id=registered.gateway_id)
    registry.upsert(WorkspaceTarget(gateway_id=registered.gateway_id, workspace_id=workspace_id,
                                   name="当前原生工作区", root_path=str(registered.directory / "workspace-0"),
                                   backend_url="http://127.0.0.1:49999", connection_kind="local"))
    created = datetime(2026, 10, 27, tzinfo=UTC).isoformat()
    files = {
        "generators/gen_old.json": {
            "generator_id": "gen_old", "name": "保留原 Generator", "execution_workspace_id": workspace_id,
            "placement": {"kind": "workspace", "workspace_id": workspace_id},
            "created_from": {"workspace_id": workspace_id, "session_id": "ses_original"},
            "created_at": created, "updated_at": created, "revision": 7,
            "trigger": {"type": "interval", "interval_seconds": 60},
            "config": {"prompt": f"原正文不能迁写: {workspace_id}"},
        },
        "generator-schedules/gen_old.json": {"definition_revision": 7, "next_run_at": "2026-10-27T00:01:00Z"},
        "generation-runs/gen_old/run_old.json": {
            "run_id": "run_old", "generator_id": "gen_old", "idempotency_key": "key_old",
            "status": "completed", "trigger_type": "interval", "scheduled_for": created,
            "execution_workspace_id": workspace_id,
        },
    }
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())
    sent: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        payload = json.loads(request.content)
        assert payload["workspace_ids"] == {workspace_id: _workspace_id(registered)}
        assert request.headers["X-BoxTeam-Workspace-Id"] == _workspace_id(registered)
        return httpx.Response(200, json={"data": {"task_id": payload["task_id"]}})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            yield GeneratorImportCase(registered, registry, client, GatewayResourceCatalogService(
                registry=registry, http_client=client,
            ), sent)
    finally:
        registry.close()


def _generator_files(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
        for directory in ("generators", "generation-runs", "generator-schedules", "migration-backups")
        for path in (root / directory).rglob("*") if path.is_file()
    }


def test_generator_cli_preflight_never_imports_or_changes_active_sources(
    generator_case: GeneratorImportCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    registered = generator_case.registered
    before, database = _generator_files(registered.root), _dump(registered.database)

    def reject_import(*_args: object, **_kwargs: object) -> None:
        pytest.fail("身份预检不能自动导入 Generator")

    monkeypatch.setattr("app.gateway.control.generator_import.import_session_generators", reject_import)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["generator_imports"] == [{
        "source": "generators/gen_old.json", "task_id": "gen_old",
        "legacy_workspace_id": generator_case.workspace_id, "source_hash": None,
        "status": "pending", "receipt_received": False, "remaining_sources": [],
    }]
    assert [item["code"] for item in report["blockers"]] == ["unsettled_generator_import"]
    assert report["mapping_complete"] is report["apply_available"] is False
    assert _generator_files(registered.root) == before and _dump(registered.database) == database
    assert generator_case.sent == [] and not (registered.root / "migration-backups").exists()


@pytest.mark.parametrize("phase", ["frozen", "lost_ack", "retiring", "unsealed", "retired"])
async def test_generator_cli_distinguishes_frozen_receipts_and_retirement_without_writes(
    generator_case: GeneratorImportCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    phase: str,
) -> None:
    case, registered = generator_case, generator_case.registered
    original_post, original_unlink = case.client.post, Path.unlink
    original_write = generator_import_owner.atomic_write_bytes

    async def post(*args, **kwargs):
        if phase == "frozen":
            raise httpx.ConnectError("目标离线")
        response = await original_post(*args, **kwargs)
        if phase == "lost_ack":
            raise httpx.ReadError("目标已接受，但应答丢失", request=response.request)
        return response

    def unlink(path: Path, *args, **kwargs):
        if phase == "retiring" and path == registered.root / "generator-schedules/gen_old.json":
            raise OSError("源文件退休中断")
        return original_unlink(path, *args, **kwargs)

    def write(path: Path, raw: bytes) -> None:
        if phase == "unsealed" and path.name == "snapshot.json":
            raise OSError("snapshot 发布前中断")
        original_write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(case.client, "post", post)
        patch.setattr(Path, "unlink", unlink)
        patch.setattr(generator_import_owner, "atomic_write_bytes", write)
        installed, = await case.run()
    assert installed["status"] == ("imported" if phase == "retired" else "failed")
    before, database, sent = _generator_files(registered.root), _dump(registered.database), len(case.sent)
    if phase == "retired":
        _cli(monkeypatch)
    else:
        with pytest.raises(SystemExit) as error:
            _cli(monkeypatch)
        assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    records = report["generator_imports"]
    if phase == "unsealed":
        assert {item["status"] for item in records} == {"pending", "invalid"}
        assert {item["code"] for item in report["blockers"]} == {"unsettled_generator_import", "invalid_generator_import"}
    else:
        record, = records
        snapshot = json.loads((case.backup / "snapshot.json").read_bytes())
        assert record["source_hash"] == snapshot["request"]["source_hash"] == case.backup.name
        assert record["receipt_received"] is (phase in {"retiring", "retired"})
        assert record["status"] == ("imported" if phase == "retired" else "pending")
        assert bool(record["remaining_sources"]) is (phase != "retired")
    assert report["mapping_complete"] is (phase == "retired")
    assert report["apply_available"] is (phase == "retired")
    assert _generator_files(registered.root) == before and _dump(registered.database) == database
    assert len(case.sent) == sent
    # 显式重入负责结算；身份预检既不重发，也不退休任何原件。
    receipt_before = (case.backup / "receipt.json").read_bytes() if phase in {"retiring", "retired"} else None
    recovered, = await case.run()
    assert recovered["status"] == "imported"
    assert len(case.sent) == (2 if phase == "lost_ack" else 1)
    assert not tuple((registered.root / "generators").glob("*.json"))
    assert registered.preview().blockers == ()
    if receipt_before is not None:
        assert (case.backup / "receipt.json").read_bytes() == receipt_before


@pytest.mark.parametrize("damage", [
    "source_bytes", "backup_bytes", "source_hash", "workspace_mapping", "owner", "receipt_hash",
    "receipt_workspace", "receipt_status", "snapshot_missing", "receipt_missing", "unreceipted_retirement",
])
async def test_generator_cli_rejects_conflicting_frozen_input_and_receipts_without_rewriting(
    generator_case: GeneratorImportCase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    damage: str,
) -> None:
    case, registered = generator_case, generator_case.registered

    async def offline(*_args: object, **_kwargs: object) -> None:
        raise httpx.ConnectError("冻结真实 owner 的导入输入")

    if damage in {"source_bytes", "unreceipted_retirement"}:
        with monkeypatch.context() as patch:
            patch.setattr(case.client, "post", offline)
            result, = await case.run()
        assert result["status"] == "failed"
    else:
        result, = await case.run()
        assert result["status"] == "imported"
    backup = case.backup
    snapshot_path, receipt_path = backup / "snapshot.json", backup / "receipt.json"
    if damage in {"source_bytes", "backup_bytes"}:
        path = (registered.root if damage == "source_bytes" else backup) / "generators/gen_old.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif damage in {"source_hash", "workspace_mapping", "owner"}:
        snapshot = json.loads(snapshot_path.read_bytes())
        if damage == "source_hash":
            snapshot["request"]["source_hash"] = "f" * 64
            replacement = backup.with_name("f" * 64)
            backup.rename(replacement)
            backup, snapshot_path = replacement, replacement / "snapshot.json"
        elif damage == "workspace_mapping":
            snapshot["request"]["workspace_ids"]["unrelated_alias"] = str(uuid4())
        else:
            snapshot["owner"] = str(uuid4())
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    elif damage.startswith("receipt_") and damage != "receipt_missing":
        receipt = json.loads(receipt_path.read_bytes())
        field = {"receipt_hash": "source_hash", "receipt_workspace": "workspace_id", "receipt_status": "status"}[damage]
        receipt[field] = "conflicting-value"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    else:
        path = {"snapshot_missing": snapshot_path, "receipt_missing": receipt_path,
                "unreceipted_retirement": registered.root / "generators/gen_old.json"}[damage]
        path.unlink()
    before, database, sent = _generator_files(registered.root), _dump(registered.database), len(case.sent)
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in report["blockers"]] == ["invalid_generator_import"]
    record, = report["generator_imports"]
    assert record["status"] == "invalid" and record["receipt_received"] is False
    assert report["mapping_complete"] is False
    failed_preview, = await case.run(preview=True)
    failed_import, = await case.run()
    assert failed_preview["status"] == failed_import["status"] == "failed"
    assert _generator_files(registered.root) == before and _dump(registered.database) == database
    assert len(case.sent) == sent


@pytest.mark.parametrize("retired", [False, True])
async def test_generator_preflight_without_registry_keeps_exact_history_and_never_creates_identity(
    generator_case: GeneratorImportCase, retired: bool,
) -> None:
    case, registered = generator_case, generator_case.registered
    if retired:
        result, = await case.run()
        assert result["status"] == "imported"
    with closing(sqlite3.connect(registered.database)) as connection:
        connection.execute("DELETE FROM gateway_workspace_registry")
        connection.execute("DELETE FROM gateway_config WHERE config_key='workspace_registry_meta'")
        connection.execute("DELETE FROM registry_meta WHERE registry_key='workspace'")
        connection.execute("DELETE FROM user_view_state")
        connection.commit()
    # Generator 历史与 registry 缺失独立验证，不混入其它导航／视图孤项。
    (registered.root / "navigation/workspace-tree.json").unlink()
    identity = registered.root / "identity.json"
    identity.unlink()
    before, database, sent = _generator_files(registered.root), _dump(registered.database), len(case.sent)
    preview = registered.preview()
    record, = preview.generator_imports
    assert record.status == ("imported" if retired else "pending")
    assert record.legacy_workspace_id == case.workspace_id
    assert [item.code for item in preview.blockers] == ([] if retired else ["unsettled_generator_import"])
    assert preview.gateway_id is preview.registry_source is None and preview.mappings == ()
    assert _generator_files(registered.root) == before and _dump(registered.database) == database
    assert len(case.sent) == sent and not identity.exists()


@pytest.mark.parametrize("change", ["source_bytes", "workspace_mapping", "archive_bytes", "invalid_definition"])
@pytest.mark.parametrize("preview", [True, False], ids=["preview", "apply"])
async def test_unsealed_generator_backup_blocks_changed_hash_and_all_unsettled_imports(
    generator_case: GeneratorImportCase, monkeypatch: pytest.MonkeyPatch, change: str, preview: bool,
) -> None:
    case, registered = generator_case, generator_case.registered
    original_write = generator_import_owner.atomic_write_bytes

    def interrupt(path: Path, raw: bytes) -> None:
        if path.name == "snapshot.json":
            raise OSError("snapshot 发布前中断，冻结 H1 原始归档")
        original_write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(generator_import_owner, "atomic_write_bytes", interrupt)
        failed, = await case.run()
    assert failed["status"] == "failed" and case.sent == []
    pending = registered.root / "migration-backups/session-generators"
    unsealed, = pending.iterdir()
    assert not (unsealed / "snapshot.json").exists()
    definition = registered.root / "generators/gen_old.json"
    original_definition = definition.read_bytes()
    if change == "source_bytes":
        definition.write_bytes(original_definition + b" \n")
    elif change == "workspace_mapping":
        # UUID 已是唯一身份；移除原 owner 后只能登记另一真实 UUID，不能重绑旧 alias。
        case.registry.remove((registered.gateway_id, case.workspace_id))
        case.registry.upsert(WorkspaceTarget(gateway_id=registered.gateway_id,
            workspace_id=_workspace_id(registered, "gw_local_1"), name="替换后的原生工作区",
            root_path=str(registered.directory / "workspace-1"), backend_url="http://127.0.0.1:49998",
            connection_kind="local"))
    elif change == "archive_bytes":
        (unsealed / "generators/gen_old.json").write_bytes(original_definition + b" \n")
    else:
        definition.write_bytes(b"{")
    # 排在 gen_old 前的另一未结算项，也不能在 H1 对账完成前先写入或 POST。
    fresh = json.loads(original_definition)
    fresh["generator_id"] = "gen_fresh"
    (definition.parent / "gen_fresh.json").write_text(json.dumps(fresh), encoding="utf-8")
    before, database = _generator_files(registered.root), _dump(registered.database)
    results = await case.run(preview=preview)
    observed = {
        "accepted": [item for item in results if item["status"] != "failed"],
        "post_count": len(case.sent),
        "backups": sorted(path.name for path in pending.iterdir()),
        "snapshots": sorted(path.relative_to(pending).as_posix() for path in pending.glob("*/snapshot.json")),
        "receipts": sorted(path.relative_to(pending).as_posix() for path in pending.glob("*/receipt.json")),
    }
    assert observed == {"accepted": [], "post_count": 0, "backups": [unsealed.name], "snapshots": [], "receipts": []}
    assert _generator_files(registered.root) == before and _dump(registered.database) == database
