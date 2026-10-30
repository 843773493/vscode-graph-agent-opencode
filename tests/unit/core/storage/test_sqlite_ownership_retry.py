"""锁句柄释放前失败必须由原 owner 保留，并可从同一关闭入口重试。"""

from __future__ import annotations

import errno
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import portalocker
import pytest

from app.core import sqlite_state
from app.core.atomic_fs import native_io_path
from tests.unit.core.storage.test_sqlite_errors import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414
)
from tests.unit.core.storage.test_sqlite_errors import (
    state_migrations as state_migrations,  # noqa: PLC0414
)
from tests.unit.core.storage.test_sqlite_errors import (
    state_path as state_path,  # noqa: PLC0414
)


@pytest.mark.parametrize("unlock_failure", [False, True], ids=["close", "unlock-and-close"])
def test_close_before_release_failure_keeps_owner_and_retries_original_handle(
    state_path: Path, state_migrations: tuple[str, ...], unlock_failure: bool,
    monkeypatch: pytest.MonkeyPatch, diagnostic_sink: Callable[[BaseException], None],
) -> None:
    original = state_path.read_bytes()
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    close_error = PermissionError(errno.EACCES, "原生关闭失败", str(lock_path))
    unlock_error = PermissionError(errno.EACCES, "原生解锁失败", str(lock_path))
    native_open = Path.open
    native_unlock = portalocker.unlock
    attempts = {"unlock": 0, "close": 0}
    fault_active = True

    class RetainedHandle:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def close(self):
            attempts["close"] += 1
            if fault_active:
                raise close_error
            self.raw.close()

    def open_lock(path, *args, **kwargs):
        raw = native_open(path, *args, **kwargs)
        return RetainedHandle(raw) if path == native_io_path(lock_path) else raw

    def unlock(handle):
        attempts["unlock"] += 1
        if fault_active and unlock_failure:
            raise unlock_error
        native_unlock(handle)

    monkeypatch.setattr(Path, "open", open_lock)
    monkeypatch.setattr(portalocker, "unlock", unlock)
    database = sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations)
    handle = database._ownership._lock_handle
    assert handle is not None
    try:
        with pytest.raises(PermissionError) as caught:
            database.close()
        diagnostic_sink(caught.value)
        assert caught.value.__cause__ is close_error and caught.value.errno == errno.EACCES
        assert caught.value.filename is None and str(lock_path) not in str(caught.value)
        assert "stage=ownership_close" in str(caught.value)
        if unlock_failure:
            projected_unlock = close_error.__context__
            assert projected_unlock is not None and projected_unlock.__cause__ is unlock_error
            assert "stage=ownership_release" in str(projected_unlock)
            diagnostic_sink(projected_unlock)
            # 两步均在原生释放前失败，实际 OS 锁必须仍然有效。
            with (
                native_open(lock_path, "a+b") as contender,
                pytest.raises(portalocker.AlreadyLocked),
            ):
                portalocker.lock(contender, portalocker.LOCK_EX | portalocker.LOCK_NB)
        assert database._ownership._lock_handle is handle and not handle.closed
        assert state_path.read_bytes() == original
        with pytest.raises(RuntimeError, match="状态库已关闭"):
            database.connection()
        fault_active = False
        database.close()
        assert handle.closed and database._ownership._lock_handle is None
        assert attempts == {"unlock": 1 + unlock_failure, "close": 2}
        database.close()
        assert attempts == {"unlock": 1 + unlock_failure, "close": 2}
    finally:
        fault_active = False
        database.close()
        # 红测时旧实现丢弃了句柄；测试自身仍须释放真实锁。
        if not handle.closed:
            handle.raw.close()

    assert state_path.read_bytes() == original
    with (
        sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations) as recovered,
        closing(recovered.connection()) as connection,
    ):
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "原始 Ω"
