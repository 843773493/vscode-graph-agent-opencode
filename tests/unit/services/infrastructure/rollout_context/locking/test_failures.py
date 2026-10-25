"""锁错误公开必要事实，原路径留诊断，失败后原准入仍可使用。"""

from __future__ import annotations

import errno
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import portalocker
import pytest

from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
    _RolloutOperationLock,
)


async def _acquire(lock: _RolloutFileLock, asynchronous: bool) -> None:
    if asynchronous:
        await lock.acquire_async()
    else:
        lock.acquire()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 符号链接环；Windows 原生锁另行实机验收")
def test_symlink_loop_keeps_native_error_in_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "loop"
    path.symlink_to(path.name)
    with pytest.raises((RuntimeError, OSError)) as caught:
        _RolloutOperationLock(path)
    assert type(caught.value) is type(caught.value.__cause__)
    assert "artifact=rollout_lock stage=resolve" in str(caught.value)
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert path.is_symlink()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_file_timeout_keeps_reason_and_releases_admission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, asynchronous: bool,
) -> None:
    path = tmp_path / "lock"
    lock = _RolloutOperationLock(path, timeout_seconds=0.01)
    with path.open("a+b") as external:
        external.write(b" ")
        external.flush()
        external.seek(0)
        portalocker.lock(external, portalocker.LOCK_EX)
        try:
            with pytest.raises(TimeoutError, match="rollout 文件锁获取超时") as caught:
                await _acquire(lock, asynchronous)
        finally:
            portalocker.unlock(external)
    assert type(caught.value) is TimeoutError and caught.value.__cause__ is None
    assert "stage=file_acquisition" in str(caught.value)
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert lock._held is None
    await _acquire(lock, asynchronous)
    lock.release()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_local_timeout_withdraws_waiter_and_keeps_original_holder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, asynchronous: bool,
) -> None:
    path = tmp_path / "lock"
    ready, release = threading.Event(), threading.Event()

    def hold():
        with _RolloutOperationLock(path):
            ready.set()
            assert release.wait(timeout=2)

    lock = _RolloutOperationLock(path, timeout_seconds=0.01)
    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold)
        try:
            assert ready.wait(timeout=1)
            with pytest.raises(TimeoutError, match="rollout 进程内写锁获取超时") as caught:
                await _acquire(lock, asynchronous)
            assert not holder.done() and lock._held is None
            assert type(caught.value) is TimeoutError and caught.value.__cause__ is None
            assert "stage=local_admission" in str(caught.value)
            assert str(path) not in str(caught.value) and str(path) in caplog.text
        finally:
            release.set()
        holder.result(timeout=1)
    await _acquire(lock, asynchronous)
    lock.release()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_missing_read_lock_preserves_type_and_can_recover(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, asynchronous: bool,
) -> None:
    path = tmp_path / "未创建的锁"
    reader = _RolloutFileLock(path, exclusive=False)
    with pytest.raises(FileNotFoundError) as caught:
        await _acquire(reader, asynchronous)
    error = caught.value
    assert error.errno == errno.ENOENT and error.filename is None
    assert type(error.__cause__) is FileNotFoundError
    assert error.__cause__.filename == str(path)
    assert "artifact=rollout_lock stage=open" in str(error)
    assert str(path) not in str(error) and str(path) in caplog.text
    assert reader._held is None and not path.exists()
    with _RolloutOperationLock(path):
        pass
    await _acquire(reader, asynchronous)
    reader.release()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_invalid_parent_does_not_strand_admission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, asynchronous: bool,
) -> None:
    parent = tmp_path / "原始文件"
    parent.write_text("保留原件")
    path = parent / "lock"
    lock = _RolloutOperationLock(path)
    with pytest.raises(FileExistsError) as caught:
        await _acquire(lock, asynchronous)
    assert caught.value.errno == errno.EEXIST
    assert isinstance(caught.value.__cause__, FileExistsError)
    assert "stage=open" in str(caught.value)
    assert str(parent) not in str(caught.value) and str(parent) in caplog.text
    assert parent.read_text() == "保留原件" and lock._held is None
    parent.unlink()
    await _acquire(lock, asynchronous)
    lock.release()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_upgrade_rejection_keeps_reader_and_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, asynchronous: bool,
) -> None:
    path = tmp_path / "lock"
    with _RolloutOperationLock(path):
        pass
    reader = _RolloutFileLock(path, exclusive=False)
    await _acquire(reader, asynchronous)
    writer = _RolloutOperationLock(path)
    with pytest.raises(RuntimeError, match="共享锁不能升级为独占锁") as caught:
        await _acquire(writer, asynchronous)
    assert "stage=reentry" in str(caught.value)
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert reader._held is not None and reader._held.depth == 1
    reader.release()
    await _acquire(writer, asynchronous)
    writer.release()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_fatal_file_lock_failure_keeps_cause_and_closes_handle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    path = tmp_path / "lock"
    failure = portalocker.LockException(f"原生锁错误 {path}")
    opened = []
    original_open = Path.open

    def record_open(target, *args, **kwargs):
        handle = original_open(target, *args, **kwargs)
        if target == path:
            opened.append(handle)
        return handle

    lock = _RolloutOperationLock(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", record_open)
        patch.setattr(portalocker, "lock", Mock(side_effect=failure))
        with pytest.raises(RuntimeError, match="stage=file_acquisition") as caught:
            await _acquire(lock, asynchronous)
    assert type(caught.value) is RuntimeError and caught.value.__cause__ is failure
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert opened and all(handle.closed for handle in opened)
    assert lock._held is None
    await _acquire(lock, asynchronous)
    lock.release()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_failed_cleanup_close_is_reported_without_stranding_admission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    path = tmp_path / "lock"
    failure = PermissionError(errno.EACCES, f"原生关闭错误 {path}", str(path))
    original_open = Path.open
    opened = []

    def open_with_failed_close(target, *args, **kwargs):
        handle = original_open(target, *args, **kwargs)
        opened.append(handle)
        wrapped = Mock(wraps=handle)

        def close():
            handle.close()
            raise failure

        wrapped.close.side_effect = close
        return wrapped

    lock = _RolloutOperationLock(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_with_failed_close)
        patch.setattr(portalocker, "lock", Mock(side_effect=portalocker.LockException("锁不可用")))
        with pytest.raises(PermissionError, match="stage=close") as caught:
            await _acquire(lock, asynchronous)
    assert caught.value.errno == errno.EACCES and caught.value.__cause__ is failure
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert opened and all(handle.closed for handle in opened)
    assert lock._held is None
    await _acquire(lock, asynchronous)
    lock.release()
