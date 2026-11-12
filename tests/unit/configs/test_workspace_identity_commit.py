"""完整旧控制面经显式维护迁移，覆盖同事务归属与维护窗口恢复。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config_migration_guard import require_settled_config_migration
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.gateway_state._schema import _GATEWAY_MIGRATIONS
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry
from configs.cli import main
from configs.maintenance import runtime_logging_commit as publisher
from configs.maintenance.workspace_identity_commit import (
    commit_workspace_identity_migration,
)
from tests.unit.configs.conftest import LegacyGateway


def commit(case: LegacyGateway, **options):
    return commit_workspace_identity_migration(gateway_root=case.root, project_root=Path.cwd(), **options)


def test_full_legacy_state_migrates_to_single_owner_and_keeps_views_and_shared_group(legacy_gateway: LegacyGateway) -> None:
    case = legacy_gateway
    before = case.dump()
    original_nodes = json.loads((case.root / "navigation/workspace-tree.json").read_text())["nodes"]
    preview = case.preview()
    assert not preview.blockers and preview.report()["apply_available"]
    result = commit(case)
    assert result["state"] == "completed"
    recovery = json.loads(Path(result["recovery_output"]).read_text())
    assert recovery == {"format_version": 1, "migration_id": result["migration_id"],
        "registry_gateway_id": case.gateway_id, "mappings": [{"legacy_workspace_id": alias,
        "gateway_id": case.gateway_id, "workspace_id": value["workspace_id"]} for alias, value in case.metadata["workspaces"].items()]}
    assert "private_key" not in json.dumps(recovery)
    with GatewayStateStore(path=case.database) as state:
        registry = GatewayWorkspaceRegistry(storage_path=case.root / "workspaces.json", state_store=state)
        try:
            assert len(registry.targets()) == 3
            assert {target.key for target in registry.targets()} == {(case.gateway_id, value["workspace_id"]) for value in case.metadata["workspaces"].values()}
            assert registry.active_workspace == (case.gateway_id, case.metadata["workspaces"]["gw_local_0"]["workspace_id"])
            owner, = registry.backend_owners()
            assert owner.contract.workspace_ids == tuple(case.metadata["workspaces"][f"gw_local_{i}"]["workspace_id"] for i in (1, 2))
            nodes = WorkspaceNavigationStore(storage_path=case.root / "navigation/workspace-tree.json").read_nodes()
            assert [(node.node_id, node.parent_node_id, node.name, node.position) for node in nodes] == [
                (node["node_id"], node["parent_node_id"], node["name"], node["position"]) for node in original_nodes]
        finally:
            registry.close()
    with closing(sqlite3.connect(case.database)) as connection:
        view, = connection.execute("SELECT gateway_id,workspace_id,turn_anchor,scroll_offset,projection_version FROM user_view_state")
        assert view == (case.gateway_id, case.metadata["workspaces"]["gw_local_0"]["workspace_id"], "正文必须保留 gw_local_0", 123.5, 7)
        assert connection.execute("SELECT version FROM schema_migrations WHERE version = 19").fetchone() == (19,)
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == len(_GATEWAY_MIGRATIONS)
    journal = json.loads((case.root / ".workspace-identity-migration.json").read_text())
    with closing(sqlite3.connect(Path(journal["backup_root"]) / "gateway.before.sqlite")) as connection:
        assert "\n".join(connection.iterdump()) == before
    after = case.dump()
    assert commit(case) == result and case.dump() == after


@pytest.mark.parametrize("failure_after", [0, 1])
def test_interrupted_publish_blocks_startup_and_resumes_original_plan(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, failure_after: int,
) -> None:
    case = legacy_gateway
    original = publisher.atomic_write
    interrupted = False

    def fail(path: Path, content: bytes, mode: int = 0o600):
        nonlocal interrupted
        if path == case.root / "navigation/workspace-tree.json" or (
            failure_after == 0 and path == case.root / ".workspace-identity-migration.json"
            and json.loads(content).get("state") == "applying"
        ):
            interrupted = True
            raise OSError("发布中断")
        return original(path, content, mode)

    with monkeypatch.context() as patch:
        patch.setattr(publisher, "atomic_write", fail)
        with pytest.raises(OSError, match="发布中断"):
            commit(case)
    assert interrupted
    with pytest.raises(RuntimeError, match="migrate-workspace-identity"):
        require_settled_config_migration(case.database)
    result = commit(case)
    assert result["state"] == "completed"
    require_settled_config_migration(case.database)
    with GatewayStateStore(path=case.database) as state:
        assert state.load_workspace_registry()["schema_version"] == 14


def test_restore_preserves_complete_before_state_and_rejects_new_writes(legacy_gateway: LegacyGateway) -> None:
    case = legacy_gateway
    before = case.dump()
    navigation = (case.root / "navigation/workspace-tree.json").read_bytes()
    commit(case)
    assert commit(case, restore=True)["state"] == "restored"
    assert case.dump() == before and (case.root / "navigation/workspace-tree.json").read_bytes() == navigation
    result = commit(case)
    with closing(sqlite3.connect(case.database)) as connection:
        connection.execute("UPDATE user_view_state SET turn_anchor='新状态不允许被旧备份覆盖'")
        connection.commit()
    changed = case.dump()
    assert commit(case) == result and case.dump() == changed
    with pytest.raises(RuntimeError, match="外部修改"):
        commit(case, restore=True)
    assert case.dump() == changed


def test_completed_migration_keeps_recovery_output_conflict_after_schema_upgrade(legacy_gateway: LegacyGateway) -> None:
    case = legacy_gateway
    result = commit(case)
    with GatewayStateStore(path=case.database) as state:
        assert state.load_workspace_registry() is not None
    after = case.dump()
    journal_path = case.root / ".workspace-identity-migration.json"
    journal = journal_path.read_bytes()
    output = case.directory / "occupied-recovery.json"
    output.write_text("保留已存在的不同恢复材料")
    original = output.read_bytes()
    with pytest.raises(ValueError, match="客户端恢复输出已存在不同内容"):
        commit(case, recovery_output=output)
    assert output.read_bytes() == original
    assert case.dump() == after and journal_path.read_bytes() == journal
    require_settled_config_migration(case.database)
    assert commit(case) == result and case.dump() == after


def test_file_only_input_is_imported_once_and_can_restore_absence(legacy_gateway: LegacyGateway) -> None:
    case = legacy_gateway
    with closing(sqlite3.connect(case.database)) as connection:
        from app.gateway.control.gateway_registry.snapshot import (
            read_workspace_registry,
        )
        payload = read_workspace_registry(connection)
    source = case.root / "workspaces.json"
    source.write_text(json.dumps(payload))
    original = source.read_bytes()
    case.database.unlink()
    first = commit(case)
    assert first["state"] == "completed"
    assert commit(case) == first
    with GatewayStateStore(path=case.database) as state:
        assert len(state.load_workspace_registry()["targets"]) == 3
    assert commit(case, restore=True)["state"] == "restored"
    assert not case.database.exists() and source.read_bytes() == original


def test_cli_exports_only_successful_explicit_migration(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    output = legacy_gateway.directory / "recovery.json"
    monkeypatch.setattr("sys.argv", ["boxteam", "migrate-workspace-identity", "--recovery-output", str(output)])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "completed" and output.exists()
    assert json.loads(output.read_text())["migration_id"] == report["migration_id"]
    expanduser = Path.expanduser
    monkeypatch.setattr(Path, "expanduser", lambda path: legacy_gateway.root if path == Path("~/selected-gateway") else expanduser(path))
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", "~/selected-gateway")
    monkeypatch.setattr("sys.argv", ["boxteam", "migrate-workspace-identity", "--verify-remote", "--recovery-output", str(output)])
    main()
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize("legacy_gateway", [True], indirect=True)
def test_unverified_remote_prevents_any_publication_or_recovery_export(legacy_gateway: LegacyGateway) -> None:
    case = legacy_gateway
    before = case.dump()
    navigation = (case.root / "navigation/workspace-tree.json").read_bytes()
    output = case.directory / "未核验不能发布.json"
    with pytest.raises(ValueError, match="未核验来源"):
        commit(case, recovery_output=output)
    assert case.dump() == before
    assert (case.root / "navigation/workspace-tree.json").read_bytes() == navigation
    assert not output.exists() and not (case.root / ".workspace-identity-migration.json").exists()


@pytest.mark.parametrize("legacy_gateway", [True], indirect=True)
@pytest.mark.parametrize("different_gateway", [False, True])
def test_verified_remote_keeps_owner_and_merges_only_same_source(
    legacy_gateway: LegacyGateway, different_gateway: bool,
) -> None:
    case = legacy_gateway
    with closing(sqlite3.connect(case.database)) as connection:
        metadata = json.loads(connection.execute(
            "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'"
        ).fetchone()[0])
        if different_gateway:
            metadata["remote_gateway_connections"][1]["remote_gateway_id"] = "gateway_" + "c" * 32
        for index, cid in enumerate(("rgw_fixture_0", "rgw_fixture_1")):
            alias = f"gw_remote_{index}"
            target = json.loads(connection.execute("SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id=?", (alias,)).fetchone()[0])
            target.update(remote_affected_workspace_ids=[alias], remote_config_event_cursor=7 + index)
            metadata["remote_gateway_connections"][index]["remote_config_event_cursor"] = 7 + index
            connection.execute("UPDATE gateway_workspace_registry SET payload_json=? WHERE workspace_id=?", (json.dumps(target), alias))
        connection.execute("UPDATE gateway_config SET payload_json=? WHERE config_key='workspace_registry_meta'", (json.dumps(metadata),))
        connection.commit()
    preview = case.preview()
    # 此用例验证维护发布消费已核验快照；认证 HTTP 核验由 remote_preflight 覆盖。
    verified = replace(preview, mappings=tuple(replace(item, verified=True) for item in preview.mappings),
                       blockers=tuple(item for item in preview.blockers if item.code != "remote_identity_unverified"))
    result = commit(case, verified_preview=verified)
    with GatewayStateStore(path=case.database) as state:
        registry = GatewayWorkspaceRegistry(storage_path=case.root / "workspaces.json", state_store=state)
        try:
            remote = [item for item in registry.targets() if item.connection_kind == "remote_gateway"]
            assert len(remote) == (2 if different_gateway else 1)
            assert {item.workspace_id for item in remote} == {"00000000-0000-4000-8000-000000000099"}
            assert {item.gateway_id for item in remote} == {"gateway_" + "b" * 32, *(["gateway_" + "c" * 32] if different_gateway else [])}
            assert sorted(cid for item in remote for cid in item.remote_gateway_connection_ids) == ["rgw_fixture_0", "rgw_fixture_1"]
            assert all(registry.remote_gateway_connection(cid).protocol_version == 2 for cid in ("rgw_fixture_0", "rgw_fixture_1"))
            assert [registry.remote_gateway_connection(cid).remote_config_event_cursor for cid in ("rgw_fixture_0", "rgw_fixture_1")] == [7, 8]
            assert all(item.remote_affected_workspace_ids == ("00000000-0000-4000-8000-000000000099",) for item in remote)
        finally:
            registry.close()
    mappings = json.loads(Path(result["recovery_output"]).read_text())["mappings"]
    assert len(mappings) == 5 and {item["legacy_workspace_id"] for item in mappings} >= {"gw_remote_0", "gw_remote_1"}


def test_changed_source_after_verification_cannot_publish_old_plan(legacy_gateway: LegacyGateway) -> None:
    case = legacy_gateway
    verified = case.preview()
    with closing(sqlite3.connect(case.database)) as connection:
        raw = json.loads(connection.execute("SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id='gw_local_0'").fetchone()[0])
        raw["name"] = "核验后更新的名称"
        connection.execute("UPDATE gateway_workspace_registry SET payload_json=? WHERE workspace_id='gw_local_0'", (json.dumps(raw),))
        connection.commit()
    before = case.dump()
    with pytest.raises(ValueError, match="预检后.*变化"):
        commit(case, verified_preview=verified)
    assert case.dump() == before and not (case.root / ".workspace-identity-migration.json").exists()


def test_tampered_candidate_blocks_resume_before_writing_any_asset(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = legacy_gateway
    before = case.dump()
    original = publisher.atomic_write

    def interrupt(path: Path, content: bytes, mode: int = 0o600):
        if path == case.root / ".workspace-identity-migration.json" and json.loads(content).get("state") == "applying":
            raise OSError("发布前中断")
        original(path, content, mode)

    with monkeypatch.context() as patch:
        patch.setattr(publisher, "atomic_write", interrupt)
        with pytest.raises(OSError, match="发布前中断"):
            commit(case)
    journal = json.loads((case.root / ".workspace-identity-migration.json").read_text())
    candidate = Path(journal["assets"][0]["after"])
    with closing(sqlite3.connect(candidate)) as connection:
        connection.execute("UPDATE user_view_state SET turn_anchor='备份已被更改'")
        connection.commit()
    with pytest.raises(RuntimeError, match="备份校验失败"):
        commit(case)
    assert case.dump() == before
    with pytest.raises(RuntimeError, match="migrate-workspace-identity"):
        require_settled_config_migration(case.database)


@pytest.mark.parametrize("legacy_gateway", [True], indirect=True)
@pytest.mark.parametrize("conflict", [False, True])
def test_merged_owner_view_conflict_is_never_silently_overwritten(
    legacy_gateway: LegacyGateway, conflict: bool,
) -> None:
    case = legacy_gateway
    with closing(sqlite3.connect(case.database)) as connection:
        for alias in ("gw_remote_0", "gw_remote_1"):
            connection.execute(
                "INSERT INTO user_view_state SELECT user_id,?,session_id,turn_anchor,scroll_offset,follow_latest,projection_version,tool_details_expanded,updated_at "
                "FROM user_view_state WHERE workspace_id='gw_local_0'", (alias,),
            )
        if conflict:
            connection.execute("UPDATE user_view_state SET turn_anchor='另一个真实视图位置' WHERE workspace_id='gw_remote_1'")
        connection.commit()
    preview = case.preview()
    verified = replace(preview, mappings=tuple(replace(item, verified=True) for item in preview.mappings),
                       blockers=tuple(item for item in preview.blockers if item.code != "remote_identity_unverified"))
    before = case.dump()
    if conflict:
        with pytest.raises(ValueError, match="用户视图.*冲突"):
            commit(case, verified_preview=verified)
        assert case.dump() == before and not (case.root / ".workspace-identity-migration.json").exists()
    else:
        commit(case, verified_preview=verified)
        with closing(sqlite3.connect(case.database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM user_view_state").fetchone()[0] == 2
            view, = connection.execute("SELECT gateway_id,turn_anchor,scroll_offset FROM user_view_state WHERE workspace_id='00000000-0000-4000-8000-000000000099'")
            assert view == ("gateway_" + "b" * 32, "正文必须保留 gw_local_0", 123.5)


def test_cli_reverifies_restored_source_before_new_migration(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from configs.maintenance import workspace_identity_migration

    first = commit(legacy_gateway)
    commit(legacy_gateway, restore=True)
    verify = workspace_identity_migration.verify_remote_workspace_identities
    observed = []

    async def checked(preview, **options):
        observed.append(preview.registry_digest)
        return await verify(preview, **options)

    monkeypatch.setattr(workspace_identity_migration, "verify_remote_workspace_identities", checked)
    monkeypatch.setattr("sys.argv", ["boxteam", "migrate-workspace-identity", "--restore", "--verify-remote"])
    main()
    assert json.loads(capsys.readouterr().out)["state"] == "restored" and observed == []
    output = legacy_gateway.directory / "new-recovery.json"
    monkeypatch.setattr("sys.argv", ["boxteam", "migrate-workspace-identity", "--verify-remote", "--recovery-output", str(output)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert len(observed) == 1 and result["state"] == "completed"
    assert result["migration_id"] != first["migration_id"]
    assert json.loads(output.read_text())["migration_id"] == result["migration_id"]
