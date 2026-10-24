"""用真实跨进程文件锁验证附件访问与维护互斥及失败释放。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog import (
    catalog as catalog_module,
)
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
    value = AttachmentBlobCatalog(attachments_root / "catalog.sqlite", workspace_id="a475d596-039d-48db-a14c-a0cbf7b4b58c")
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
    second = AttachmentBlobCatalog(catalog.database_path, workspace_id=catalog.workspace_id)
    try:
        assert catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert second.connection.execute("SELECT count(*) FROM attachment_directories").fetchone()[0] == 0
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
        assert catalog._connections == {}
    assert catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 4


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


@pytest.mark.parametrize("failure_stage", ["path_budget", "connect", "schema"])
def test_failed_open_releases_access_and_preserves_original_error(
    catalog: AttachmentBlobCatalog, probe: Callable[[bool], int],
    monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    failure = OSError("受控的连接或 schema 失败")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    with monkeypatch.context() as patch:
        if failure_stage == "connect":
            patch.setattr(sqlite3, "connect", fail)
        else:
            patch.setattr(catalog_module, "validate_sqlite_path_budget" if failure_stage == "path_budget" else "initialize_directory_schema", fail)
        with pytest.raises(OSError) as caught:
            _ = catalog.connection
        assert caught.value is failure
        assert probe(True) == 0
    assert catalog.connection.execute("PRAGMA user_version").fetchone()[0] == 4


@pytest.mark.parametrize("version", [1, 2, 3, 99])
def test_unknown_schema_rejection_does_not_leak_online_lock(
    catalog: AttachmentBlobCatalog, probe: Callable[[bool], int], version: int,
) -> None:
    catalog.database_path.parent.mkdir(parents=True)
    with sqlite3.connect(catalog.database_path) as connection:
        connection.execute(f"PRAGMA user_version = {version}")
    with pytest.raises(RuntimeError, match="只支持 schema 4"):
        _ = catalog.connection
    assert probe(True) == 0


@pytest.mark.parametrize("failed_during_init", [False, True])
def test_unconfirmed_close_keeps_lock_until_successful_retry(
    catalog: AttachmentBlobCatalog, probe: Callable[[bool], int],
    monkeypatch: pytest.MonkeyPatch, failed_during_init: bool,
) -> None:
    connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    class FailingClose(sqlite3.Connection):
        fail = False

        def close(self) -> None:
            if self.fail:
                raise OSError("连接关闭未核实")
            super().close()

    def capture_connection(*args, **kwargs) -> sqlite3.Connection:
        connection = connect(*args, **kwargs, factory=FailingClose)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture_connection)
    try:
        if failed_during_init:
            failure = ValueError("初始化未完成")

            def fail_init(connection: FailingClose, **_kwargs: object) -> None:
                connection.fail = True
                raise failure

            monkeypatch.setattr(catalog_module, "initialize_directory_schema", fail_init)
            with pytest.raises(ValueError) as caught:
                _ = catalog.connection
            assert caught.value is failure
            assert "连接关闭未核实" in " ".join(failure.__notes__)
        else:
            connection = catalog.connection
            connection.fail = True
            with pytest.raises(OSError, match="连接关闭未核实"):
                catalog.close()
        (connection,) = opened
        assert probe(True) == 73
        with pytest.raises(RuntimeError, match="已关闭"):
            _ = catalog.connection
    finally:
        FailingClose.fail = False
        for opened_connection in opened:
            opened_connection.fail = False
        catalog.close()
    assert probe(True) == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_another_thread_open_failure_keeps_existing_connection_and_maintenance_lock(
    catalog: AttachmentBlobCatalog, probe: Callable[[bool], int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = catalog.connection
    failure = OSError("另一个线程连接失败")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", fail)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(lambda: catalog.connection)
            with pytest.raises(OSError) as caught:
                pending.result(timeout=5)
        assert caught.value is failure
    assert catalog.connection is connection
    assert connection.execute("SELECT count(*) FROM attachment_directories").fetchone()[0] == 0
    assert probe(True) == 73
    catalog.close()
    assert probe(True) == 0


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
