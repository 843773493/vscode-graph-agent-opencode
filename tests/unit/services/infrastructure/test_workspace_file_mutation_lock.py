"""所有文件入口共用的有界进程内与跨进程写锁。"""

from __future__ import annotations

import os
from pathlib import Path

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


@pytest.mark.skipif(os.name == "nt", reason="仅在 POSIX 上覆盖 fcntl 文件锁")
def test_apply_patch_file_lock_times_out_when_cross_process_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    original_flock = fcntl.flock

    def reject_nonblocking_lock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_NB:
            raise BlockingIOError("test lock is held")
        original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", reject_nonblocking_lock)
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


