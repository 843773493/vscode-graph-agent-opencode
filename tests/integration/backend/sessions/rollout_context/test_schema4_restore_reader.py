"""真实 fresh4 backup/restore 与存活 SQLite reader 的 inode/WAL 一致性。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext


@dataclass(frozen=True)
class RestoreCase:
    saver: RolloutCheckpointSaver
    session: str
    plan: ContextRequestPlan
    index: Path
    backup: Path
    context: TestRunContext


@pytest.fixture
def restore_case(
    request: pytest.FixtureRequest,
    session_bundle_factory: Callable[[Path, str], Path],
) -> RestoreCase:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session = f"ses_{uuid4().hex}"
    session_bundle_factory(sessions, session)
    saver = RolloutCheckpointSaver(sessions)
    saver.accept_turn(
        session,
        accepted_ingress_id="restore-ingress",
        acceptance_idempotency_key="restore-acceptance",
        payload="恢复备份不能创建同名双 inode",
    )
    plan = ContextRequestPlan(
        session_id=session,
        plan_id="restore-plan",
        plan_creation_idempotency_key="restore-create",
        refs=(),
    )
    registered = saver.create_context_plan(session, plan)
    assert registered.revision == 0 and registered.plan_state == "unsealed"
    with saver._storage._connect(session, read_only=True) as connection:
        assert connection.execute(
            "SELECT schema_version FROM database_meta"
        ).fetchone() == (4,)
    backup = saver._storage.backup_index(
        session,
        destination=context.artifacts_dir / f"{session}.before.sqlite",
    )
    return RestoreCase(
        saver, session, plan, saver._storage.index_path(session), backup, context
    )


def _files(case: RestoreCase) -> dict[str, object]:
    paths = (case.index, Path(str(case.index) + "-wal"), Path(str(case.index) + "-shm"))
    files = {
        path.name: {"inode": path.stat().st_ino, "size": path.stat().st_size}
        for path in paths
        if path.exists()
    }
    descriptors: dict[str, object] = {}
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = fd.readlink()
        except FileNotFoundError:
            continue
        if str(case.index) in str(target):
            descriptors[fd.name] = {"target": str(target), "inode": fd.stat().st_ino}
    return {"files": files, "fds": descriptors}


def _observe(connection: sqlite3.Connection) -> dict[str, object]:
    try:
        row = connection.execute(
            "SELECT revision, draft_json FROM context_plans WHERE plan_id='restore-plan'"
        ).fetchone()
        assert row is not None
        return {
            "revision": row[0],
            "history_view_revision": json.loads(row[1])["history_view_revision"],
            "integrity": connection.execute("PRAGMA integrity_check").fetchall(),
        }
    except sqlite3.DatabaseError as error:
        # 诊断中保留错误；调用方仍断言精确成功状态，不把错误降级成 PASS。
        return {"error": str(error)}


def _restore_with_reader(case: RestoreCase, reader_mode: str) -> dict[str, object]:
    reader = None
    evidence: dict[str, object] = {
        "reader_mode": reader_mode,
        "sqlite_version": sqlite3.sqlite_version,
    }
    original_jsonl = case.saver._storage.jsonl_path(case.session).read_bytes()
    original_backup = case.backup.read_bytes()
    backup_stat = case.backup.stat()
    artifact = case.context.artifacts_dir / f"{case.session}.restore.json"
    try:
        if reader_mode != "closed":
            reader = sqlite3.connect(
                f"{case.index.as_uri()}?mode={reader_mode}", uri=True
            )
            assert _observe(reader)["revision"] == 0
            assert not reader.in_transaction
        evidence["before_write"] = _files(case)
        revised = case.saver.revise_context_plan(
            case.session,
            replace(case.plan, history_view_revision=7),
            expected_revision=0,
        )
        assert revised.revision == 1
        if reader is not None:
            assert _observe(reader)["revision"] == 1
            assert not reader.in_transaction
        evidence["before_restore"] = _files(case)
        with case.saver._storage.restore_index_backup(
            case.session, case.backup
        ) as restored:
            evidence["restored_snapshot"] = _observe(restored.connection)
            evidence["while_snapshot_open"] = _files(case)
        evidence["after_restore"] = _files(case)
        if reader is not None:
            evidence["old_reader"] = _observe(reader)
            assert not reader.in_transaction
        restarted = RolloutCheckpointSaver(case.saver._storage.sessions_dir)
        with restarted._storage._connect(case.session, read_only=True) as fresh:
            evidence["new_connection"] = _observe(fresh)
        registered = restarted.get_context_plan_registration(
            case.session, plan_id=case.plan.plan_id
        )
        evidence["new_saver_revision"] = registered.revision
        evidence["after_new_saver"] = _files(case)
        # 只有确实恢复到备份后才尝试合法写入；已经错读时不继续污染证据。
        if registered.revision == 0:
            written = restarted.revise_context_plan(
                case.session,
                replace(case.plan, history_view_revision=11),
                expected_revision=0,
            )
            evidence["post_restore_write"] = {"revision": written.revision}
            if reader is not None:
                evidence["old_reader_after_write"] = _observe(reader)
            with restarted._storage._connect(case.session, read_only=True) as fresh:
                evidence["new_connection_after_write"] = _observe(fresh)
        assert (
            case.saver._storage.jsonl_path(case.session).read_bytes() == original_jsonl
        )
        evidence["jsonl_unchanged"] = True
        assert case.backup.read_bytes() == original_backup
        assert case.backup.stat().st_ino == backup_stat.st_ino
        assert case.backup.stat().st_mtime_ns == backup_stat.st_mtime_ns
        evidence["backup_unchanged"] = True
        return evidence
    finally:
        evidence["final_files"] = _files(case)
        artifact.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        if reader is not None:
            reader.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc inode 与存活 FD 证据")
@pytest.mark.parametrize("reader_mode", ["closed", "ro", "rw"])
def test_restore_really_restores_backup_with_idle_reader(restore_case, reader_mode):
    evidence = _restore_with_reader(restore_case, reader_mode)
    expected = {"revision": 0, "history_view_revision": 0, "integrity": [("ok",)]}
    assert evidence["restored_snapshot"] == expected
    assert evidence["new_connection"] == expected
    assert evidence["new_saver_revision"] == 0
    if reader_mode != "closed":
        assert evidence["old_reader"] == expected
    assert evidence["post_restore_write"] == {"revision": 1}
    after_write = {"revision": 1, "history_view_revision": 11, "integrity": [("ok",)]}
    assert evidence["new_connection_after_write"] == after_write
    if reader_mode != "closed":
        assert evidence["old_reader_after_write"] == after_write
    assert evidence["jsonl_unchanged"] is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc inode 与存活 FD 证据")
@pytest.mark.parametrize("reader_mode", ["ro", "rw"])
def test_restore_cannot_detach_live_database_reader(restore_case, reader_mode):
    evidence = _restore_with_reader(restore_case, reader_mode)
    before_inode = evidence["before_restore"]["files"]["index.sqlite"]["inode"]
    after_inode = evidence["after_restore"]["files"]["index.sqlite"]["inode"]
    detached = [
        record
        for record in evidence["after_restore"]["fds"].values()
        if record["target"].endswith("index.sqlite (deleted)")
    ]
    assert not detached, f"restore 成功返回却遗留旧 database inode: {detached}"
    assert after_inode == before_inode


@pytest.mark.parametrize("reader_mode", ["ro", "rw"])
def test_restore_preserves_active_wal_snapshot(restore_case, reader_mode):
    case = restore_case
    storage = case.saver._storage
    original_jsonl = storage.jsonl_path(case.session).read_bytes()
    original_backup = case.backup.read_bytes()
    inode = case.index.stat().st_ino
    revised = case.saver.revise_context_plan(
        case.session, replace(case.plan, history_view_revision=7), expected_revision=0
    )
    assert revised.revision == 1
    with closing(
        sqlite3.connect(f"{case.index.as_uri()}?mode={reader_mode}", uri=True)
    ) as reader:
        assert reader.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        reader.execute("BEGIN")
        before = _observe(reader)
        assert before == {
            "revision": 1,
            "history_view_revision": 7,
            "integrity": [("ok",)],
        }
        with storage.restore_index_backup(case.session, case.backup) as snapshot:
            restored = _observe(snapshot.connection)
            assert restored == {
                "revision": 0,
                "history_view_revision": 0,
                "integrity": [("ok",)],
            }
        # 活动读事务保留旧快照；不能为了强制看到恢复值而重开 reader。
        assert reader.in_transaction
        assert _observe(reader) == before
        if reader_mode == "rw":
            with pytest.raises(sqlite3.OperationalError) as caught:
                reader.execute("UPDATE context_plans SET revision=99")
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        reader.rollback()
        assert _observe(reader) == restored
        written = case.saver.revise_context_plan(
            case.session,
            replace(case.plan, history_view_revision=11),
            expected_revision=0,
        )
        assert written.revision == 1
        after_write = _observe(reader)
        assert after_write == {
            "revision": 1,
            "history_view_revision": 11,
            "integrity": [("ok",)],
        }
    assert case.index.stat().st_ino == inode
    assert storage.jsonl_path(case.session).read_bytes() == original_jsonl
    assert case.backup.read_bytes() == original_backup
    (case.context.artifacts_dir / f"{case.session}.snapshot.json").write_text(
        json.dumps(
            {
                "reader_mode": reader_mode,
                "before": before,
                "restored": restored,
                "after_write": after_write,
                "inode": inode,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_restore_accepts_readonly_backup_without_changing_it(restore_case):
    case = restore_case
    original = case.backup.read_bytes()
    before = case.backup.stat()
    case.backup.chmod(0o444)
    try:
        with case.saver._storage.restore_index_backup(
            case.session, case.backup
        ) as snapshot:
            assert _observe(snapshot.connection)["revision"] == 0
        assert case.backup.read_bytes() == original
        assert case.backup.stat().st_mtime_ns == before.st_mtime_ns
        assert case.backup.stat().st_ino == before.st_ino
        assert case.backup.stat().st_mode & 0o777 == 0o444
    finally:
        case.backup.chmod(before.st_mode & 0o777)


def test_restore_rejects_readonly_target_without_modification(restore_case):
    case = restore_case
    original = case.index.read_bytes()
    before = case.index.stat()
    original_backup = case.backup.read_bytes()
    case.index.chmod(0o444)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly") as caught:
            case.saver._storage.restore_index_backup(case.session, case.backup)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
        assert str(case.index) in " ".join(caught.value.__notes__)
        assert case.index.read_bytes() == original
        assert case.index.stat().st_ino == before.st_ino
        assert case.backup.read_bytes() == original_backup
    finally:
        case.index.chmod(before.st_mode & 0o777)


@pytest.mark.parametrize("payload", [b"not-a-sqlite-database", b""])
def test_restore_rejects_invalid_source_before_target_write(restore_case, payload):
    case = restore_case
    broken = case.context.artifacts_dir / f"{case.session}.invalid.sqlite"
    broken.write_bytes(payload)
    before = case.index.read_bytes()
    inode = case.index.stat().st_ino
    expected_error = sqlite3.DatabaseError if payload else RuntimeError
    with pytest.raises(expected_error, match="not a database|空数据库"):
        case.saver._storage.restore_index_backup(case.session, broken)
    assert broken.read_bytes() == payload
    assert case.index.read_bytes() == before
    assert case.index.stat().st_ino == inode
    with case.saver._storage._connect(case.session, read_only=True) as connection:
        assert _observe(connection)["revision"] == 0


@pytest.mark.parametrize(
    "alias_kind",
    ["same", "hardlink", "symlink", "parent_symlink", "wal_symlink", "target_symlink"],
)
def test_restore_rejects_unsafe_source_or_sidecar(restore_case, alias_kind):
    case = restore_case
    source = case.index
    original = case.index.read_bytes()
    inode = case.index.stat().st_ino
    original_backup = case.backup.read_bytes()
    artifact = case.context.artifacts_dir / f"{case.session}.alias"
    expected_error = ValueError
    if alias_kind == "hardlink":
        artifact.hardlink_to(case.index)
        source = artifact
    elif alias_kind == "symlink":
        artifact.symlink_to(case.backup)
        source, expected_error = artifact, RuntimeError
    elif alias_kind == "parent_symlink":
        artifact.symlink_to(case.backup.parent, target_is_directory=True)
        source, expected_error = artifact / case.backup.name, RuntimeError
    elif alias_kind == "wal_symlink":
        Path(f"{case.index}-wal").symlink_to(case.backup)
        source, expected_error = case.backup, RuntimeError
    elif alias_kind == "target_symlink":
        case.index.rename(artifact)
        case.index.symlink_to(artifact)
        source, expected_error = case.backup, RuntimeError
    with pytest.raises(expected_error, match="同一文件|符号链接"):
        case.saver._storage.restore_index_backup(case.session, source)
    assert case.index.read_bytes() == original
    assert case.index.stat().st_ino == inode
    assert case.backup.read_bytes() == original_backup


@pytest.mark.parametrize("violation", ["check", "foreign_key"])
def test_restore_validates_source_integrity_before_writing_target(
    restore_case, violation
):
    case = restore_case
    broken = case.context.artifacts_dir / f"{case.session}.invalid-check.sqlite"
    original_backup = case.backup.read_bytes()
    with (
        closing(sqlite3.connect(f"{case.backup.as_uri()}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(broken)) as destination,
    ):
        source.backup(destination)
        if violation == "check":
            destination.execute("PRAGMA ignore_check_constraints=ON")
            destination.execute("UPDATE context_plans SET revision=-1")
        else:
            destination.execute(
                "INSERT INTO context_plan_refs VALUES (?, 'missing-plan', 'request_only', 'orphan', '{}')",
                (case.session,),
            )
        destination.commit()
        destination.execute("PRAGMA ignore_check_constraints=OFF")
        if violation == "check":
            assert destination.execute("PRAGMA integrity_check").fetchall() == [
                ("CHECK constraint failed in context_plans",)
            ]
        else:
            assert destination.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert destination.execute("PRAGMA foreign_key_check").fetchall() == [
                ("context_plan_refs", 1, "context_plans", 0)
            ]
    broken_before = broken.read_bytes()
    target_before = case.index.read_bytes()
    inode = case.index.stat().st_ino
    check_name = "integrity_check" if violation == "check" else "foreign_key_check"
    with pytest.raises(RuntimeError, match=f"source {check_name}.*context_plans"):
        case.saver._storage.restore_index_backup(case.session, broken)
    assert broken.read_bytes() == broken_before
    assert case.backup.read_bytes() == original_backup
    assert case.index.read_bytes() == target_before
    assert case.index.stat().st_ino == inode


def test_restore_does_not_create_missing_target(restore_case):
    case = restore_case
    preserved = case.context.artifacts_dir / f"{case.session}.preserved.sqlite"
    original = case.index.read_bytes()
    inode = case.index.stat().st_ino
    case.index.rename(preserved)
    with pytest.raises(FileNotFoundError):
        case.saver._storage.restore_index_backup(case.session, case.backup)
    assert not case.index.exists()
    assert preserved.read_bytes() == original
    assert preserved.stat().st_ino == inode


def test_restore_rejects_corrupt_target_without_replacement(restore_case):
    case = restore_case
    original_backup = case.backup.read_bytes()
    original_jsonl = case.saver._storage.jsonl_path(case.session).read_bytes()
    # 当前 fixture 所有连接已关闭；仅在独立正式工作区注入真实损坏。
    case.index.write_bytes(b"corrupted")
    before = case.index.stat()
    unrelated = case.index.with_name(f".index.sqlite.{case.session}.restore")
    unrelated.write_bytes(b"belongs-to-another-operation")
    with pytest.raises(sqlite3.DatabaseError, match="not a database") as caught:
        case.saver._storage.restore_index_backup(case.session, case.backup)
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert str(case.index) in " ".join(caught.value.__notes__)
    assert str(case.backup) in " ".join(caught.value.__notes__)
    assert case.index.read_bytes() == b"corrupted"
    assert case.index.stat().st_ino == before.st_ino
    assert case.index.stat().st_mtime_ns == before.st_mtime_ns
    assert case.backup.read_bytes() == original_backup
    assert case.saver._storage.jsonl_path(case.session).read_bytes() == original_jsonl
    assert unrelated.read_bytes() == b"belongs-to-another-operation"
    (case.context.artifacts_dir / f"{case.session}.corrupt-target.json").write_text(
        json.dumps(
            {
                "error": str(caught.value),
                "notes": caught.value.__notes__,
                "sqlite_errorcode": caught.value.sqlite_errorcode,
                "inode": before.st_ino,
                "target_unchanged": True,
                "backup_unchanged": True,
                "jsonl_unchanged": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("operation", ["initialize", "open_read_snapshot"])
def test_corrupt_target_enters_explicit_recovery_required(restore_case, operation):
    case = restore_case
    original_backup = case.backup.read_bytes()
    case.index.write_bytes(b"corrupted")
    before = case.index.stat()

    action = getattr(case.saver._storage, operation)
    with pytest.raises(
        RuntimeError,
        match="recovery_required.*SQLite backup.*禁止从 JSONL 重建",
    ) as caught:
        action(case.session)

    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
    assert case.index.read_bytes() == b"corrupted"
    assert case.index.stat().st_ino == before.st_ino
    assert case.index.stat().st_mtime_ns == before.st_mtime_ns
    assert case.backup.read_bytes() == original_backup


def test_offline_restore_rejects_readable_target_without_replacement(restore_case):
    case = restore_case
    original_target = case.index.read_bytes()
    original_backup = case.backup.read_bytes()
    inode = case.index.stat().st_ino

    with pytest.raises(RuntimeError, match="只允许恢复无法打开的损坏 target"):
        case.saver._storage.restore_index_backup_offline(case.session, case.backup)

    assert case.index.read_bytes() == original_target
    assert case.index.stat().st_ino == inode
    assert case.backup.read_bytes() == original_backup
    assert not (case.index.parent / "recovery-quarantine").exists()
    assert not tuple(case.index.parent.glob(".index.sqlite.*.offline-restore"))


def test_offline_restore_rejects_invalid_source_before_quarantine(restore_case):
    case = restore_case
    case.index.write_bytes(b"corrupted-target")
    before = case.index.stat()
    broken = case.context.artifacts_dir / f"{case.session}.invalid-offline.sqlite"
    broken.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        case.saver._storage.restore_index_backup_offline(case.session, broken)

    assert case.index.read_bytes() == b"corrupted-target"
    assert case.index.stat().st_ino == before.st_ino
    assert case.index.stat().st_mtime_ns == before.st_mtime_ns
    assert broken.read_bytes() == b"not-a-sqlite-database"
    quarantine_root = case.index.parent / "recovery-quarantine"
    assert not quarantine_root.exists() or not tuple(quarantine_root.iterdir())
    assert not tuple(case.index.parent.glob(".index.sqlite.*.offline-restore"))


def test_restore_fails_promptly_when_another_writer_holds_target(restore_case):
    case = restore_case
    original_backup = case.backup.read_bytes()
    inode = case.index.stat().st_ino
    with closing(sqlite3.connect(case.index)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        before = _observe(writer)
        # 独立进程设置硬超时，真实锁冲突不能落入 Python backup 的无限重试。
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from app.services.infrastructure.rollout_context.storage.service "
                    "import RolloutStorage; RolloutStorage(sys.argv[1]).restore_index_backup(sys.argv[2], sys.argv[3])"
                ),
                str(case.saver._storage.sessions_dir),
                case.session,
                str(case.backup),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        (case.context.artifacts_dir / f"{case.session}.locked-target.txt").write_text(
            result.stderr, encoding="utf-8"
        )
        assert result.returncode == 1
        assert "SQLite restore database is locked: status=5" in result.stderr
        assert str(case.index) in result.stderr and str(case.backup) in result.stderr
        assert _observe(writer) == before
        assert writer.in_transaction
        writer.rollback()
    assert case.index.stat().st_ino == inode
    assert case.backup.read_bytes() == original_backup
    with case.saver._storage.restore_index_backup(
        case.session, case.backup
    ) as snapshot:
        assert _observe(snapshot.connection) == before
