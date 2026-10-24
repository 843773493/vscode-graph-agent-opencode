"""产品创建和消息准入生成旧库，离线备份保留原件、队列与 canonical 历史。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from app.abstractions.workspace_resources import ResourceKind
from app.services.business.workspace_resources.records import (
    ForkAttachReceipt,
    OwnedResourceRecord,
    ResourceRecordStore,
    ThreadRef,
)
from app.services.infrastructure.attachment_blob_catalog.migration import backup
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    BackupSource,
    checked_path,
    file_digest,
)
from tests.integration.backend.sessions.attachment_migration.legacy_inputs import (
    BODY,
    BackupCase,
)

MODULE = "app.services.infrastructure.attachment_blob_catalog.migration"


@pytest.fixture(params=["terminal", "browser"])
def offline_owner_record(
    backup_case: BackupCase, request: pytest.FixtureRequest,
) -> tuple[BackupCase, ResourceRecordStore, OwnedResourceRecord]:
    # 本组只测离线记录准入和字节保全，不充当真实外部进程停止验收。
    kind = cast(ResourceKind, request.param)
    root = backup_case.workspace
    identity = json.loads((root / ".boxteam/workspace-identity.json").read_bytes())["workspace_id"]
    store = ResourceRecordStore(workspace_root=root, workspace_id=identity, kind=kind)
    stamp = datetime.now(UTC).isoformat()
    record = OwnedResourceRecord(
        resource_id=("term_" if kind == "terminal" else "browser_") + "a" * 32,
        workspace_id=identity, source_thread=ThreadRef(backup_case.session_id, backup_case.thread_id),
        retention_scope="workspace", title="离线资源记录", created_at=stamp, updated_at=stamp,
        status="completed" if kind == "terminal" else "closed", reconciliation_status="verified",
    )
    store.records[record.resource_id] = record
    return backup_case, store, record


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", MODULE, *args], capture_output=True, text=True, timeout=40, check=False)


async def test_prepare_and_verify_via_cli_preserve_product_history_and_pending_input(backup_case: BackupCase) -> None:
    case = backup_case
    source_hash = file_digest(case.original)
    result = cli("prepare", "--workspace", str(case.workspace), "--output", str(case.output))
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["state"] == "prepared" and response["source_attachment_schema"] == 3
    assert str(case.workspace) not in result.stdout and str(case.output) not in result.stdout
    verified = cli("verify", "--backup", str(case.output))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == response
    root = case.workspace / ".boxteam"
    restored_index = case.output / "data" / case.index_path.relative_to(root)
    with closing(sqlite3.connect(restored_index.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        assert tuple(connection.execute("SELECT item_id, content_hash FROM item_catalog ORDER BY item_sequence")) == case.canonical_items
        assert connection.execute("SELECT phase FROM execution_queue").fetchone() == ("queued",)
    assert (case.output / "data" / case.original.relative_to(root)).read_bytes() == BODY
    assert file_digest(case.original) == source_hash
    with closing(sqlite3.connect(root / "attachments/catalog.sqlite")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    assert not list(case.output.rglob("*-wal"))
    assert not list(case.output.rglob("*-shm"))
    manifest = json.loads((case.output / "manifest.json").read_bytes())
    assert all(not Path(row["locator"]).is_absolute() and str(case.workspace) not in row["locator"] for row in manifest["files"])


async def test_live_wal_is_included_by_sqlite_backup_api(backup_case: BackupCase) -> None:
    case = backup_case
    # WAL 输入是 SQLite 持久化格式测试；业务 history 已由上方产品入口生成。
    with closing(sqlite3.connect(case.index_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE backup_wal_probe (value TEXT)")
        writer.execute("INSERT INTO backup_wal_probe VALUES ('committed in WAL')")
        writer.commit()
        assert case.index_path.with_name("index.sqlite-wal").stat().st_size > 0
        result = backup.prepare_backup(case.workspace, case.output)
        assert result["state"] == "prepared"
        snapshot = case.output / "data" / case.index_path.relative_to(case.workspace / ".boxteam")
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            assert connection.execute("SELECT value FROM backup_wal_probe").fetchone() == ("committed in WAL",)


@pytest.mark.parametrize("corruption", [
    "original", "schema", "missing_table", "missing_column", "missing_trigger", "missing_index",
    "active_ingest", "active_commit", "active_fork",
])
async def test_invalid_input_is_rejected_before_any_backup_is_published(backup_case: BackupCase, corruption: str) -> None:
    case = backup_case
    database = case.workspace / ".boxteam/attachments/catalog.sqlite"
    if corruption == "original":
        case.original.write_bytes(b"damaged source")
    else:
        with closing(sqlite3.connect(database)) as connection:
            if corruption == "schema":
                connection.execute("PRAGMA user_version=2")
            elif corruption == "missing_table":
                connection.execute("DROP TABLE fork_attachment_mappings")
            elif corruption == "missing_column":
                connection.execute("ALTER TABLE fork_attachment_mappings DROP COLUMN snapshot_json")
            elif corruption == "missing_trigger":
                connection.execute("DROP TRIGGER attachment_revision_attachment_blobs_insert")
            elif corruption == "missing_index":
                connection.execute("DROP INDEX idx_attachment_owner_refs_blob")
            elif corruption == "active_ingest":
                connection.execute("UPDATE attachment_ingest_records SET state='hashed'")
            elif corruption == "active_commit":
                connection.execute("UPDATE attachment_blob_commit_claims SET state='claimed'")
            else:
                connection.execute("INSERT INTO fork_attachment_claims VALUES ('claim','operation','snapshot','attachment','owner','session','thread','blob','{}','hash','hash','preparing')")
            connection.commit()
    before = file_digest(case.original)
    result = cli("prepare", "--workspace", str(case.workspace), "--output", str(case.output))
    assert result.returncode != 0
    assert not case.output.exists()
    assert file_digest(case.original) == before


@pytest.mark.parametrize("phase", ["claimed", "started", "terminal"])
async def test_unsettled_execution_is_rejected(backup_case: BackupCase, phase: str) -> None:
    case = backup_case
    with closing(sqlite3.connect(case.index_path)) as connection:
        # 模拟领取/启动落库后的崩溃；必须保留完整 claim，不能制造违反表约束的假状态。
        # terminal 队列搭配 unknown outcome 仍无终态证明；queued 才允许完整保留待执行输入。
        connection.execute(
            "UPDATE execution_queue SET phase=?, claim_owner='crashed-runtime', claim_generation=1", (phase,),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="收敛操作|尚未核实的执行"):
        backup.prepare_backup(case.workspace, case.output)
    assert not case.output.exists()


@pytest.mark.parametrize("state", ["active", "reconcile_required", "unexpected"])
async def test_unknown_external_operation_is_not_treated_as_stopped(backup_case: BackupCase, state: str) -> None:
    case = backup_case
    ledger = case.workspace / ".boxteam/resources.json"
    ledger.write_text(json.dumps({"records": [], "leases": [{"status": state}]}))
    with pytest.raises(RuntimeError, match="未核实的外部资源"):
        backup.prepare_backup(case.workspace, case.output)
    assert not case.output.exists()


async def test_released_external_lease_requires_its_resource_record(backup_case: BackupCase) -> None:
    case = backup_case
    (case.workspace / ".boxteam/resources.json").write_text(json.dumps({
        "records": [], "leases": [{"status": "released", "resource_id": "missing-resource"}],
    }))
    with pytest.raises(RuntimeError, match="缺少资源记录"):
        backup.prepare_backup(case.workspace, case.output)
    assert not case.output.exists()


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_owner_creation_before_ledger_registration_blocks_offline_backup(
    legacy_case_factory, kind: str,
) -> None:
    case = legacy_case_factory(kind)
    source = case.workspace / f".boxteam/{kind}-resources.json"
    original = source.read_bytes()
    (record,) = json.loads(original)["records"]
    assert record["status"] == "creating" and record["reconciliation_status"] == "reconcile_required"
    ledger = case.workspace / ".boxteam/resources.json"
    if ledger.exists():
        assert record["resource_id"] not in {row["resource_id"] for row in json.loads(ledger.read_bytes())["records"]}
    with pytest.raises(RuntimeError, match="外部资源 owner 尚未收敛"):
        backup.prepare_backup(case.workspace, case.output)
    assert not case.output.exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize("status,reconciliation", [
    ("running", "verified"), ("closed", "unknown"),
    ("deleting", "verified"), ("deleted", "reconcile_required"),
])
async def test_owner_record_requires_both_terminal_and_verified(
    offline_owner_record: tuple[BackupCase, ResourceRecordStore, OwnedResourceRecord],
    status: str, reconciliation: str,
) -> None:
    case, store, record = offline_owner_record
    record.status, record.reconciliation_status = status, reconciliation
    store.persist()
    original = store.path.read_bytes()
    with pytest.raises(RuntimeError, match="外部资源 owner 尚未收敛"):
        backup.prepare_backup(case.workspace, case.output)
    assert not case.output.exists()
    assert store.path.read_bytes() == original


async def test_terminal_owner_record_is_preserved_without_mutating_associations(
    offline_owner_record: tuple[BackupCase, ResourceRecordStore, OwnedResourceRecord],
) -> None:
    case, store, record = offline_owner_record
    record.thread_associations.append(record.source_thread)
    store.persist()
    original = store.path.read_bytes()
    response = backup.prepare_backup(case.workspace, case.output)
    assert response == backup.verify_backup(case.output)
    assert store.path.read_bytes() == original
    assert (case.output / "data" / store.path.name).read_bytes() == original


async def test_terminal_owner_with_dispatching_fork_attach_is_rejected(
    offline_owner_record: tuple[BackupCase, ResourceRecordStore, OwnedResourceRecord],
) -> None:
    case, store, record = offline_owner_record
    record.fork_attachments["pending-fork"] = ForkAttachReceipt(
        creation_key="pending-fork", target_thread=record.source_thread,
        expected_revision=record.owner_revision, state="dispatching", lease_id="unsettled-lease",
    )
    store.persist()
    with pytest.raises(RuntimeError, match="fork attach 尚未收敛"):
        backup.prepare_backup(case.workspace, case.output)
    assert not case.output.exists()


async def test_copy_failure_keeps_incomplete_backup_and_source(backup_case: BackupCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = backup_case
    copy = backup._copy
    copies = 0

    def fail_second(source: BackupSource, target: Path) -> dict[str, object]:
        nonlocal copies
        copies += 1
        if copies == 2:
            raise OSError("受控的复制中断")
        return copy(source, target)

    monkeypatch.setattr(backup, "_copy", fail_second)
    with pytest.raises(OSError, match="复制中断"):
        backup.prepare_backup(case.workspace, case.output)
    assert case.original.read_bytes() == BODY
    assert json.loads((case.output / "journal.json").read_text())["state"] == "preparing"
    assert list((case.output / "data").rglob("*"))
    with pytest.raises(RuntimeError, match="尚未完成"):
        backup.verify_backup(case.output)
    with pytest.raises(FileExistsError):
        backup.prepare_backup(case.workspace, case.output)


async def test_committed_wal_change_during_copy_prevents_publication(
    backup_case: BackupCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = backup_case
    copy = backup._copy
    with closing(sqlite3.connect(case.index_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE backup_wal_probe (value TEXT)")
        writer.commit()

        def change_after_copy(source: BackupSource, target: Path) -> dict[str, object]:
            result = copy(source, target)
            if source.path == case.index_path:
                writer.execute("INSERT INTO backup_wal_probe VALUES ('late commit')")
                writer.commit()
            return result

        monkeypatch.setattr(backup, "_copy", change_after_copy)
        with pytest.raises(RuntimeError, match="输入发生变化"):
            backup.prepare_backup(case.workspace, case.output)
    assert json.loads((case.output / "journal.json").read_text())["state"] == "preparing"
    with pytest.raises(RuntimeError, match="尚未完成"):
        backup.verify_backup(case.output)


@pytest.mark.parametrize("damage", ["file", "manifest", "journal"])
async def test_verify_rejects_changed_backup(backup_case: BackupCase, damage: str) -> None:
    case = backup_case
    backup.prepare_backup(case.workspace, case.output)
    if damage == "file":
        path = case.output / "data" / case.original.relative_to(case.workspace / ".boxteam")
        path.write_bytes(b"changed backup")
    elif damage == "manifest":
        path = case.output / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    else:
        path = case.output / "journal.json"
        payload = json.loads(path.read_bytes())
        payload["state"] = "preparing"
        path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError):
        backup.verify_backup(case.output)


def test_backup_locator_rejects_escape_and_symlink(tmp_path: Path) -> None:
    for locator in ("../outside", "/absolute", "C:/target", "C:target", "nested/C:/target", "nested/C:target", "a\\b", "a//b"):
        with pytest.raises(ValueError):
            checked_path(tmp_path, locator)
    (tmp_path / "outside").mkdir()
    (tmp_path / "data").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(RuntimeError, match="符号链接"):
        checked_path(tmp_path / "data", "file")
