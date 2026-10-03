"""既有 UUIDv4 canonical ID 的显式隔离与报告验收。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from app.core import session_catalog_migration as migration_package
from app.core.session_catalog_legacy_layout import (
    FOLDER_MANIFEST_NAME,
    SESSION_MANIFEST_NAME,
)
from app.core.session_catalog_migration import (
    QuarantinedNode,
    SessionCatalogMigrationError,
)
from scripts import migrate_session_catalog as runner
from tests.unit.core.test_session_catalog_migration import (
    MigrationWorkspace,
    _index_record,
    _write_folder_dir,
    _write_index,
    _write_session_dir,
    collect_store_node_ids,
    date_bucket_dir,
    journal_physical,
    make_migrator,
    make_session_id,
    open_catalog,
    read_journal,
)

_SUGGESTED_ACTION = "人工确认节点 ID（修正旧 index 记录或目录名）后重跑迁移"


@pytest.fixture
def workspace() -> MigrationWorkspace:
    root = (
        Path.cwd()
        / "out/tests/unit/core/test_session_catalog_migration_legacy_uuid4_quarantine/workspace"
        / uuid.uuid4().hex
    )
    return MigrationWorkspace(root=root / ".boxteam")


def _canonical_uuid4_session_id() -> str:
    value = uuid.uuid4()
    assert value.version == 4
    assert value.variant == uuid.RFC_4122
    return f"ses_{value.hex}"


def _build_mixed_workspace(
    workspace: MigrationWorkspace, *, kind: str
) -> tuple[str, str]:
    legacy_id = _canonical_uuid4_session_id()
    current_id = make_session_id()
    if kind == "session":
        _write_session_dir(
            workspace.sessions_root,
            legacy_id,
            title="存量 UUIDv4 会话",
            parent_session_id=None,
        )
        legacy_record = _index_record(legacy_id, "session", "存量 UUIDv4 会话", None)
    else:
        _write_folder_dir(workspace.sessions_root, legacy_id)
        legacy_record = _index_record(legacy_id, "folder", "存量 UUIDv4 文件夹", None)
    _write_session_dir(
        workspace.sessions_root,
        current_id,
        title="当前 UUIDv7 会话",
        parent_session_id=None,
    )
    _write_index(
        workspace.index_path,
        [
            legacy_record,
            _index_record(current_id, "session", "当前 UUIDv7 会话", None),
        ],
    )
    return legacy_id, current_id


@pytest.mark.parametrize("kind", ["session", "folder"])
async def test_canonical_uuid4_nodes_are_quarantined_without_absorbing_uuid7(
    workspace: MigrationWorkspace,
    kind: str,
) -> None:
    legacy_id, current_id = _build_mixed_workspace(workspace, kind=kind)

    result = await make_migrator(workspace).migrate()

    assert result.quarantined_nodes == (
        QuarantinedNode(node_id=legacy_id, reason="illegal_id"),
    )
    assert result.migrated_session_nodes == 1
    assert result.migrated_folder_nodes == 0
    assert read_journal(workspace)["state"] == "completed"
    target = workspace.root / "orphaned" / "session-catalog-migration" / legacy_id
    assert target.is_dir()
    assert not (workspace.sessions_root / legacy_id).exists()
    if kind == "session":
        assert not date_bucket_dir(workspace, legacy_id).exists()

    sessions, folders = journal_physical(workspace)
    records = sessions if kind == "session" else folders
    assert records[legacy_id]["state"] == "quarantine_isolated"
    assert records[legacy_id]["classification"] == "quarantine"
    assert {item.node_id: item.reason for item in result.quarantined_nodes}[
        legacy_id
    ] == "illegal_id"

    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {current_id}
        with pytest.raises(KeyError):
            store.get_node(legacy_id)
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["session", "folder"])
def test_runner_json_and_human_reports_expose_uuid4_disposition(
    workspace: MigrationWorkspace,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    legacy_id, _current_id = _build_mixed_workspace(workspace, kind=kind)
    workspace_root = workspace.root.parent
    json_exit_code = runner.main(
        ["--workspace-root", str(workspace_root), "--json"]
    )
    json_payload = json.loads(capsys.readouterr().out)
    expected_path = str(workspace_root / ".boxteam" / "sessions" / legacy_id)
    assert json_exit_code == 0
    assert json_payload["quarantined_nodes"] == [
        {
            "node_id": legacy_id,
            "reason": "illegal_id",
            "path": expected_path,
            "suggested_action": _SUGGESTED_ACTION,
        }
    ]

    human_exit_code = runner.main(["--workspace-root", str(workspace_root)])
    human_output = capsys.readouterr().out
    assert human_exit_code == 0
    assert legacy_id in human_output
    assert "illegal_id" in human_output
    assert expected_path in human_output
    assert _SUGGESTED_ACTION in human_output


@pytest.mark.parametrize("kind", ["session", "folder"])
async def test_canonical_uuid4_quarantine_recovers_after_rename_before_fsync(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    legacy_id, current_id = _build_mixed_workspace(workspace, kind=kind)
    source = workspace.sessions_root / legacy_id
    target = workspace.root / "orphaned" / "session-catalog-migration" / legacy_id
    real_rename = os.rename

    def rename_then_interrupt(
        source_path: str | os.PathLike[str],
        target_path: str | os.PathLike[str],
    ) -> None:
        real_rename(source_path, target_path)
        if Path(source_path) == source:
            raise OSError("simulated crash after rename, before directory fsync")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            migration_package._quarantine.os, "rename", rename_then_interrupt
        )
        with pytest.raises(SessionCatalogMigrationError, match="隔离区失败"):
            await make_migrator(workspace).migrate()

    sessions, folders = journal_physical(workspace)
    records = sessions if kind == "session" else folders
    assert records[legacy_id]["state"] == "quarantine_intent"
    assert not source.exists()
    assert target.is_dir()
    manifest_name = (
        SESSION_MANIFEST_NAME if kind == "session" else FOLDER_MANIFEST_NAME
    )
    assert (target / manifest_name).is_file()

    result = await make_migrator(workspace).migrate()

    assert result.quarantined_nodes == (
        QuarantinedNode(node_id=legacy_id, reason="illegal_id"),
    )
    assert not source.exists()
    assert target.is_dir()
    assert (target / manifest_name).is_file()
    assert read_journal(workspace)["state"] == "completed"
    store = open_catalog(workspace)
    try:
        assert collect_store_node_ids(store) == {current_id}
    finally:
        store.close()
