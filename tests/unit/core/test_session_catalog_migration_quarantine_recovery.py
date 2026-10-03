"""quarantine 目录 rename 与 journal checkpoint 之间的恢复验证。"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

from app.core import session_catalog_migration as migration_package
from app.core.session_catalog_migration import (
    SessionCatalogMigrationError,
)
from app.core.session_catalog_migration._contracts import _MigrationContext
from app.core.session_catalog_migration._journal import (
    SessionCatalogMigratorJournalMixin,
)
from app.core.session_catalog_migration._quarantine import (
    SessionCatalogMigratorQuarantineMixin,
    _tree_sha256,
)
from tests.unit.core.test_session_catalog_migration import (
    MigrationWorkspace,
    _index_record,
    _write_folder_dir,
    _write_index,
    _write_session_dir,
    make_migrator,
    read_journal,
)


@pytest.fixture
def workspace() -> MigrationWorkspace:
    root = (
        Path.cwd()
        / "out/tests/unit/core/test_session_catalog_migration_quarantine_recovery/workspace"
        / uuid.uuid4().hex
    )
    return MigrationWorkspace(root=root)


def _build_quarantine_node(
    workspace: MigrationWorkspace, *, kind: str, empty_directories: bool = False
) -> tuple[str, Path]:
    node_id = f"job_{uuid.uuid4().hex}"
    node_path = workspace.sessions_root / node_id
    if kind == "session":
        _write_session_dir(
            workspace.sessions_root,
            node_id,
            title="非法会话",
            parent_session_id=None,
        )
        if empty_directories:
            (node_path / "empty" / "nested").mkdir(parents=True)
        record = _index_record(node_id, "session", "非法会话", None)
    else:
        _write_folder_dir(workspace.sessions_root, node_id)
        record = _index_record(node_id, "folder", "非法文件夹", None)
    _write_index(workspace.index_path, [record])
    return node_id, node_path


def _physical_record(
    workspace: MigrationWorkspace, node_id: str, kind: str
) -> dict[str, object]:
    raw = read_journal(workspace)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    mapping = physical["sessions" if kind == "session" else "folders"]
    assert isinstance(mapping, dict)
    record = mapping[node_id]
    assert isinstance(record, dict)
    return record


def _target_path(workspace: MigrationWorkspace, node_id: str) -> Path:
    return workspace.root / "orphaned" / "session-catalog-migration" / node_id


async def _interrupt_after_quarantine_rename(
    workspace: MigrationWorkspace,
    node_id: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_rename = os.rename

    def rename_then_interrupt(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        real_rename(source, target)
        raise OSError("simulated crash after rename")

    with monkeypatch.context() as patcher:
        patcher.setattr(migration_package._quarantine.os, "rename", rename_then_interrupt)
        with pytest.raises(SessionCatalogMigrationError, match="隔离区失败"):
            await make_migrator(workspace).migrate()
    record = _physical_record(workspace, node_id, kind)
    assert record["state"] == "quarantine_intent"
    assert not (workspace.sessions_root / node_id).exists()
    assert _target_path(workspace, node_id).is_dir()


async def _interrupt_before_quarantine_rename(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(_source: str | os.PathLike[str], _target: str | os.PathLike[str]) -> None:
        raise OSError("simulated crash before rename")

    with monkeypatch.context() as patcher:
        patcher.setattr(migration_package._quarantine.os, "rename", interrupt)
        with pytest.raises(SessionCatalogMigrationError, match="隔离区失败"):
            await make_migrator(workspace).migrate()


@pytest.mark.parametrize("kind", ["session", "folder"])
async def test_quarantine_resume_after_rename_before_journal_write(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """session 与 folder 都能从 intent + 目标侧证据恢复 rename 后崩溃。"""
    node_id, _ = _build_quarantine_node(workspace, kind=kind)
    await _interrupt_after_quarantine_rename(workspace, node_id, kind, monkeypatch)

    await make_migrator(workspace).migrate()

    record = _physical_record(workspace, node_id, kind)
    assert record["state"] == "quarantine_isolated"
    assert isinstance(record.get("quarantine_intent"), dict)
    assert read_journal(workspace)["state"] == "completed"


@pytest.mark.parametrize("kind", ["session", "folder"])
async def test_quarantine_resume_when_intent_is_durable_but_source_remains(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """intent 落盘后、rename 前中断时从唯一源侧继续。"""
    node_id, source = _build_quarantine_node(workspace, kind=kind)
    await _interrupt_before_quarantine_rename(workspace, monkeypatch)

    record = _physical_record(workspace, node_id, kind)
    assert record["state"] == "quarantine_intent"
    assert source.is_dir()
    assert not _target_path(workspace, node_id).exists()

    await make_migrator(workspace).migrate()

    assert not source.exists()
    assert _target_path(workspace, node_id).is_dir()
    assert _physical_record(workspace, node_id, kind)["state"] == "quarantine_isolated"


@pytest.mark.parametrize("presence", ["both", "neither"])
async def test_quarantine_resume_fails_when_intent_has_no_unique_location(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    presence: str,
) -> None:
    node_id, source = _build_quarantine_node(workspace, kind="session")
    await _interrupt_after_quarantine_rename(
        workspace, node_id, "session", monkeypatch
    )
    target = _target_path(workspace, node_id)
    if presence == "both":
        shutil.copytree(target, source)
    else:
        shutil.rmtree(target)

    with pytest.raises(SessionCatalogMigrationError, match="必须恰有一侧存在"):
        await make_migrator(workspace).migrate()


async def test_quarantine_resume_rejects_tampered_target_content(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id, source = _build_quarantine_node(workspace, kind="session")
    (source / "payload.bin").write_bytes(b"original")
    await _interrupt_after_quarantine_rename(
        workspace, node_id, "session", monkeypatch
    )
    ( _target_path(workspace, node_id) / "payload.bin").write_bytes(b"changed")

    with pytest.raises(SessionCatalogMigrationError, match="完整内容证据不一致"):
        await make_migrator(workspace).migrate()


async def test_quarantine_resume_rejects_symlink_in_source_tree(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id, source = _build_quarantine_node(workspace, kind="session")
    await _interrupt_before_quarantine_rename(workspace, monkeypatch)
    (source / "linked.json").symlink_to(source / "session.json")

    with pytest.raises(SessionCatalogMigrationError, match="含符号链接"):
        await make_migrator(workspace).migrate()

    assert source.is_dir()
    assert not _target_path(workspace, node_id).exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="平台不支持 FIFO")
async def test_quarantine_resume_rejects_special_file_in_source_tree(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id, source = _build_quarantine_node(workspace, kind="session")
    await _interrupt_before_quarantine_rename(workspace, monkeypatch)
    os.mkfifo(source / "agent.pipe")

    with pytest.raises(SessionCatalogMigrationError, match="非普通文件"):
        await make_migrator(workspace).migrate()

    assert source.is_dir()
    assert not _target_path(workspace, node_id).exists()


async def test_quarantine_replaces_a_preexisting_empty_target(
    workspace: MigrationWorkspace,
) -> None:
    node_id, _ = _build_quarantine_node(workspace, kind="session")
    target = _target_path(workspace, node_id)
    target.mkdir(parents=True)

    await make_migrator(workspace).migrate()

    assert (target / "session.json").is_file()
    assert _physical_record(workspace, node_id, "session")["state"] == (
        "quarantine_isolated"
    )


async def test_quarantine_parent_directory_barriers_precede_rename(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _node_id, _ = _build_quarantine_node(workspace, kind="session")
    orphaned_root = workspace.root / "orphaned"
    events: list[tuple[str, Path]] = []
    real_fsync = migration_package._quarantine._fsync_directory
    real_rename = os.rename

    def record_fsync(directory: Path) -> None:
        events.append(("fsync", directory))
        real_fsync(directory)

    def record_rename(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        events.append(("rename", Path(target)))
        real_rename(source, target)

    with monkeypatch.context() as patcher:
        patcher.setattr(migration_package._quarantine, "_fsync_directory", record_fsync)
        patcher.setattr(migration_package._quarantine.os, "rename", record_rename)
        await make_migrator(workspace).migrate()

    rename_index = next(index for index, event in enumerate(events) if event[0] == "rename")
    barriers = {
        path for action, path in events[:rename_index] if action == "fsync"
    }
    assert workspace.root in barriers
    assert orphaned_root in barriers
    assert (orphaned_root / "session-catalog-migration").is_dir()


async def test_quarantine_parent_barrier_failure_preserves_source_and_intent(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id, source = _build_quarantine_node(workspace, kind="session")
    orphaned_root = workspace.root / "orphaned"
    real_fsync = migration_package._quarantine._fsync_directory
    real_rename = os.rename
    orphaned_barriers = 0
    renames = 0

    def fail_second_orphaned_barrier(directory: Path) -> None:
        nonlocal orphaned_barriers
        if directory == orphaned_root:
            orphaned_barriers += 1
            if orphaned_barriers == 2:
                raise OSError("simulated parent durability failure")
        real_fsync(directory)

    def record_rename(
        source_path: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal renames
        renames += 1
        real_rename(source_path, target)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            migration_package._quarantine,
            "_fsync_directory",
            fail_second_orphaned_barrier,
        )
        patcher.setattr(migration_package._quarantine.os, "rename", record_rename)
        with pytest.raises(SessionCatalogMigrationError, match="durability fsync 失败"):
            await make_migrator(workspace).migrate()

    assert source.is_dir()
    assert not _target_path(workspace, node_id).exists()
    assert _physical_record(workspace, node_id, "session")["state"] == (
        "quarantine_intent"
    )
    assert renames == 0


@pytest.mark.parametrize("kind", ["session", "folder"])
async def test_target_only_recovery_fsyncs_both_parents_before_isolated_checkpoint(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    node_id, source = _build_quarantine_node(workspace, kind=kind)
    await _interrupt_after_quarantine_rename(workspace, node_id, kind, monkeypatch)

    target = _target_path(workspace, node_id)
    events: list[tuple[str, Path]] = []
    real_fsync = migration_package._quarantine._fsync_directory
    real_verify = SessionCatalogMigratorQuarantineMixin._verify_quarantine_tree
    real_write = SessionCatalogMigratorJournalMixin._write_journal_context
    renames = 0

    def record_fsync(directory: Path) -> None:
        events.append(("fsync", directory))
        real_fsync(directory)

    def record_verify(
        self: SessionCatalogMigratorQuarantineMixin,
        directory: Path,
        intent: dict[str, object],
        *,
        node_id: str,
        record: dict[str, object],
        kind: str,
        stage: str,
    ) -> None:
        events.append(("proof", directory))
        real_verify(
            self,
            directory,
            intent,
            node_id=node_id,
            record=record,
            kind=kind,
            stage=stage,
        )

    def record_write(
        self: SessionCatalogMigratorJournalMixin,
        context: _MigrationContext,
        *,
        state: str,
        result: dict[str, object] | None,
    ) -> None:
        physical_sessions = context.physical.get("sessions")
        physical_folders = context.physical.get("folders")
        records = (
            physical_sessions if kind == "session" else physical_folders
        )
        physical_record = records.get(node_id) if isinstance(records, dict) else None
        if isinstance(physical_record, dict) and physical_record.get("state") == (
            "quarantine_isolated"
        ):
            events.append(("isolated_checkpoint", self._journal_path))
        real_write(self, context, state=state, result=result)

    def reject_rename(
        _source_path: str | os.PathLike[str],
        _target_path: str | os.PathLike[str],
    ) -> None:
        nonlocal renames
        renames += 1
        raise AssertionError("target-only 恢复不得再次 rename")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            migration_package._quarantine, "_fsync_directory", record_fsync
        )
        patcher.setattr(
            SessionCatalogMigratorQuarantineMixin,
            "_verify_quarantine_tree",
            record_verify,
        )
        patcher.setattr(
            SessionCatalogMigratorJournalMixin,
            "_write_journal_context",
            record_write,
        )
        patcher.setattr(migration_package._quarantine.os, "rename", reject_rename)
        await make_migrator(workspace).migrate()

    target_parent_index = events.index(("fsync", target.parent))
    source_parent_index = events.index(("fsync", source.parent))
    proof_index = events.index(("proof", target))
    checkpoint_index = events.index(("isolated_checkpoint", workspace.journal_path))
    assert target_parent_index < source_parent_index < proof_index < checkpoint_index
    assert sum(event == ("proof", target) for event in events[:checkpoint_index]) == 1
    assert _physical_record(workspace, node_id, kind)["state"] == (
        "quarantine_isolated"
    )
    assert target.is_dir()
    assert not source.exists()
    assert renames == 0


@pytest.mark.parametrize("kind", ["session", "folder"])
@pytest.mark.parametrize("failed_parent", ["target", "source"])
async def test_target_only_recovery_barrier_failure_keeps_intent_without_rename(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failed_parent: str,
) -> None:
    node_id, source = _build_quarantine_node(workspace, kind=kind)
    await _interrupt_after_quarantine_rename(workspace, node_id, kind, monkeypatch)

    target = _target_path(workspace, node_id)
    failed_path = target.parent if failed_parent == "target" else source.parent
    real_fsync = migration_package._quarantine._fsync_directory
    renames = 0

    def fail_barrier(directory: Path) -> None:
        if directory == failed_path:
            raise OSError(f"simulated {failed_parent} parent fsync failure")
        real_fsync(directory)

    def reject_rename(
        _source_path: str | os.PathLike[str],
        _target_path: str | os.PathLike[str],
    ) -> None:
        nonlocal renames
        renames += 1
        raise AssertionError("target-only 恢复不得再次 rename")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            migration_package._quarantine, "_fsync_directory", fail_barrier
        )
        patcher.setattr(migration_package._quarantine.os, "rename", reject_rename)
        with pytest.raises(SessionCatalogMigrationError, match="durability fsync 失败"):
            await make_migrator(workspace).migrate()

    record = _physical_record(workspace, node_id, kind)
    assert record["state"] == "quarantine_intent"
    assert target.is_dir()
    assert not source.exists()
    assert renames == 0
    intent = record["quarantine_intent"]
    assert isinstance(intent, dict)
    actual_tree = make_migrator(workspace)._collect_quarantine_tree(
        target, node_id=node_id, stage="测试"
    )
    assert actual_tree == intent["tree"]
    assert _tree_sha256(actual_tree) == intent["tree_sha256"]


async def test_quarantine_intent_records_and_verifies_empty_directories(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id, _ = _build_quarantine_node(
        workspace, kind="session", empty_directories=True
    )
    await _interrupt_after_quarantine_rename(
        workspace, node_id, "session", monkeypatch
    )

    record = _physical_record(workspace, node_id, "session")
    intent = record["quarantine_intent"]
    assert isinstance(intent, dict)
    tree = intent["tree"]
    assert isinstance(tree, list)
    assert {entry["path"] for entry in tree if isinstance(entry, dict)} >= {
        "empty",
        "empty/nested",
    }
    await make_migrator(workspace).migrate()
    assert (_target_path(workspace, node_id) / "empty" / "nested").is_dir()


async def test_quarantine_intent_requires_matching_target_path(
    workspace: MigrationWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id, _ = _build_quarantine_node(workspace, kind="session")
    await _interrupt_before_quarantine_rename(workspace, monkeypatch)
    raw = read_journal(workspace)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    sessions = physical["sessions"]
    assert isinstance(sessions, dict)
    record = sessions[node_id]
    assert isinstance(record, dict)
    intent = record["quarantine_intent"]
    assert isinstance(intent, dict)
    intent["target_relative_path"] = "orphaned/other/target"
    workspace.journal_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(SessionCatalogMigrationError, match="quarantine_intent 非法"):
        await make_migrator(workspace).migrate()


async def test_completed_quarantine_without_intent_fails_closed(
    workspace: MigrationWorkspace,
) -> None:
    node_id, _ = _build_quarantine_node(workspace, kind="session")
    await make_migrator(workspace).migrate()
    raw = read_journal(workspace)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    sessions = physical["sessions"]
    assert isinstance(sessions, dict)
    record = sessions[node_id]
    assert isinstance(record, dict)
    record.pop("quarantine_intent")
    workspace.journal_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(SessionCatalogMigrationError, match="缺少 durable intent"):
        await make_migrator(workspace).migrate()


async def test_active_quarantine_isolated_without_intent_fails_closed(
    workspace: MigrationWorkspace,
) -> None:
    node_id, _ = _build_quarantine_node(workspace, kind="session")
    await make_migrator(workspace).migrate()
    raw = read_journal(workspace)
    raw["state"] = "physical_migrated"
    raw.pop("result", None)
    physical = raw["physical"]
    assert isinstance(physical, dict)
    sessions = physical["sessions"]
    assert isinstance(sessions, dict)
    record = sessions[node_id]
    assert isinstance(record, dict)
    record.pop("quarantine_intent")
    workspace.journal_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(SessionCatalogMigrationError, match="缺少 durable intent"):
        await make_migrator(workspace).migrate()
