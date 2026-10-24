"""用真实跨进程文件锁验证附件访问与维护互斥及失败释放。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)

_CHILD = """
import sys
from pathlib import Path
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError, AttachmentMaintenanceLock,
)
try:
    with AttachmentMaintenanceLock(Path(sys.argv[1]), exclusive=sys.argv[2] == 'exclusive'):
        if len(sys.argv) > 3:
            Path(sys.argv[3]).write_text('locked')
            sys.stdin.read()
except AttachmentMaintenanceBusyError:
    raise SystemExit(73)
"""


@pytest.fixture
def attachments_root(tmp_path: Path) -> Path:
    return tmp_path / "workspace" / ".boxteam" / "attachments"


@pytest.fixture
def catalog(attachments_root: Path) -> Iterator[AttachmentBlobCatalog]:
    value = AttachmentBlobCatalog(attachments_root / "catalog.sqlite")
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def probe(attachments_root: Path) -> Callable[[bool], int]:
    def run(exclusive: bool) -> int:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, str(attachments_root),
             "exclusive" if exclusive else "shared"],
            capture_output=True, text=True, timeout=8, check=False,
        )
        assert result.returncode in {0, 73}, result.stderr
        return result.returncode
    return run


def test_lazy_catalog_does_not_create_state(catalog: AttachmentBlobCatalog, attachments_root: Path) -> None:
    assert not attachments_root.parent.exists()
    catalog.close()
    assert not attachments_root.parent.exists()


def test_all_online_catalogs_must_close_before_maintenance(
    catalog: AttachmentBlobCatalog, attachments_root: Path, probe: Callable[[bool], int],
) -> None:
    second = AttachmentBlobCatalog(catalog.database_path)
    try:
        assert catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert second.connection.execute("SELECT count(*) FROM attachment_blobs").fetchone()[0] == 0
        assert probe(False) == 0
        assert probe(True) == 73
        catalog.close()
        assert probe(True) == 73
    finally:
        second.close()
    assert probe(True) == 0


def test_maintenance_blocks_catalog_before_database_creation_then_allows_retry(
    catalog: AttachmentBlobCatalog, attachments_root: Path,
) -> None:
    with AttachmentMaintenanceLock(attachments_root, exclusive=True):
        with pytest.raises(AttachmentMaintenanceBusyError, match="在线访问"):
            _ = catalog.connection
        assert not attachments_root.exists()
        assert catalog._connection is None
    assert catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("state", ["preparing", "ready", "rolling_back", "installed", "aborted"])
def test_migration_journal_never_allows_missing_catalog_to_be_initialized(
    catalog: AttachmentBlobCatalog, attachments_root: Path, probe: Callable[[bool], int], state: str,
) -> None:
    attachments_root.parent.mkdir(parents=True)
    journal = attachments_root.with_name("attachment-migration.json")
    journal.write_text(json.dumps({"format_version": 1, "state": state}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="尚未完成|catalog 缺失"):
        _ = catalog.connection
    assert catalog._connection is None
    assert not attachments_root.exists()
    assert probe(True) == 0


def test_access_only_guards_without_creating_database_or_originals(
    catalog: AttachmentBlobCatalog, attachments_root: Path, probe: Callable[[bool], int],
) -> None:
    catalog.acquire_access()
    assert not attachments_root.exists()
    assert probe(True) == 73
    catalog.close()
    assert probe(True) == 0
    with pytest.raises(RuntimeError, match="已关闭"):
        catalog.acquire_access()


@pytest.mark.parametrize("failure_stage", ["connect", "schema"])
def test_failed_open_releases_access_and_preserves_original_error(
    catalog: AttachmentBlobCatalog, probe: Callable[[bool], int],
    monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    failure = OSError("受控的连接或 schema 失败")

    def fail(*_args: object) -> None:
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(catalog, "_connect" if failure_stage == "connect" else "_initialize", fail)
        with pytest.raises(OSError) as caught:
            _ = catalog.connection
        assert caught.value is failure
        assert probe(True) == 0
    assert catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_unknown_schema_rejection_does_not_leak_online_lock(
    catalog: AttachmentBlobCatalog, probe: Callable[[bool], int],
) -> None:
    catalog.database_path.parent.mkdir(parents=True)
    with sqlite3.connect(catalog.database_path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema 版本未知"):
        _ = catalog.connection
    assert probe(True) == 0


@pytest.mark.parametrize("failed_during_init", [False, True])
def test_unconfirmed_close_keeps_lock_until_successful_retry(
    catalog: AttachmentBlobCatalog, probe: Callable[[bool], int],
    monkeypatch: pytest.MonkeyPatch, failed_during_init: bool,
) -> None:
    connection = catalog.connection

    class FailingClose:
        fail = True

        def close(self) -> None:
            if self.fail:
                raise OSError("连接关闭未核实")
            connection.close()

    boundary = FailingClose()
    if failed_during_init:
        catalog._connection = None
        failure = ValueError("初始化未完成")

        def fail_init(_connection: object) -> None:
            raise failure

        monkeypatch.setattr(catalog, "_connect", lambda: boundary)
        monkeypatch.setattr(catalog, "_initialize", fail_init)
        with pytest.raises(ValueError) as caught:
            _ = catalog.connection
        assert caught.value is failure
        assert "连接关闭未核实" in " ".join(failure.__notes__)
    else:
        catalog._connection = boundary
        with pytest.raises(OSError, match="连接关闭未核实"):
            catalog.close()
    assert probe(True) == 73
    with pytest.raises(RuntimeError, match="已关闭"):
        _ = catalog.connection
    boundary.fail = False
    catalog.close()
    assert probe(True) == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("exclusive", [False, True])
def test_process_loss_releases_lock_without_removing_stable_file(
    attachments_root: Path, tmp_path: Path, probe: Callable[[bool], int], exclusive: bool,
) -> None:
    ready = tmp_path / "child-ready"
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD, str(attachments_root),
         "exclusive" if exclusive else "shared", str(ready)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    lock = AttachmentMaintenanceLock(attachments_root, exclusive=True)
    try:
        deadline = time.monotonic() + 8
        while not ready.exists():
            assert child.poll() is None, child.communicate(timeout=2)
            assert time.monotonic() < deadline, "子进程未取得锁"
            time.sleep(0.01)
        inode = lock.path.stat().st_ino
        assert probe(True) == 73
        assert probe(False) == (73 if exclusive else 0)
        child.kill()
        child.communicate(timeout=8)
        assert probe(True) == 0
        assert lock.path.stat().st_ino == inode
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=8)


def test_lock_error_closes_descriptor_without_hiding_original(
    attachments_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import portalocker

    failure = PermissionError("操作系统拒绝加锁")
    descriptors: list[int] = []

    def fail_lock(fd: int, _flags: object) -> None:
        descriptors.append(fd)
        raise failure

    monkeypatch.setattr(portalocker, "lock", fail_lock)
    with pytest.raises(PermissionError) as caught:
        AttachmentMaintenanceLock(attachments_root, exclusive=True).acquire()
    assert caught.value is failure
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
