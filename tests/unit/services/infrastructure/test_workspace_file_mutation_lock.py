"""所有文件入口共用的有界进程内与跨进程写锁。"""

from __future__ import annotations

from pathlib import Path

import portalocker
import pytest

from app.services.infrastructure.workspace_files import mutation_lock


def test_apply_patch_lock_times_out_when_same_process_writer_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path.resolve()
    lock = mutation_lock._get_workspace_lock(workspace_root)
    monkeypatch.setattr(
        mutation_lock,
        "MUTATION_LOCK_TIMEOUT_SECONDS",
        0.01,
    )
    assert lock.acquire()
    try:
        with pytest.raises(
            TimeoutError,
            match="文件写入进程内锁获取超时",
        ), mutation_lock.workspace_file_mutation_lock(workspace_root):
            pass
    finally:
        lock.release()


def test_apply_patch_file_lock_times_out_when_cross_process_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def reject_nonblocking_lock(_lock_file, flags: int) -> None:
        nonlocal attempts
        attempts += 1
        assert flags & portalocker.LOCK_NB
        raise portalocker.AlreadyLocked("test lock is held")

    monkeypatch.setattr(
        mutation_lock.portalocker,
        "lock",
        reject_nonblocking_lock,
    )
    monkeypatch.setattr(
        mutation_lock,
        "MUTATION_LOCK_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(
        TimeoutError,
        match="文件写入跨进程锁获取超时",
    ), mutation_lock.workspace_file_mutation_lock(tmp_path.resolve()):
        pass
    assert attempts > 1
