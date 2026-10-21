"""Workspace 内文件写入的短临界区；不把锁当作外部文件系统 CAS。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import portalocker

_workspace_locks_guard = threading.Lock()
_workspace_locks: dict[Path, threading.Lock] = {}
_LOCK_PATH = Path(".boxteam") / "locks" / "files.write.lock"
MUTATION_LOCK_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05


def _get_workspace_lock(workspace_root: Path) -> threading.Lock:
    with _workspace_locks_guard:
        lock = _workspace_locks.get(workspace_root)
        if lock is None:
            lock = threading.Lock()
            _workspace_locks[workspace_root] = lock
        return lock


@contextmanager
def workspace_file_mutation_lock(workspace_root: Path) -> Iterator[None]:
    """同时串行化同进程和多工作区后端进程的文件写入。"""
    thread_lock = _get_workspace_lock(workspace_root)
    acquired = thread_lock.acquire(timeout=MUTATION_LOCK_TIMEOUT_SECONDS)
    if not acquired:
        raise TimeoutError(
            "文件写入进程内锁获取超时: "
            f"timeout_seconds={MUTATION_LOCK_TIMEOUT_SECONDS:g}"
        )
    try:
        lock_path = workspace_root / _LOCK_PATH
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            _acquire_workspace_file_lock(lock_file)
            try:
                yield
            finally:
                _release_workspace_file_lock(lock_file)
    finally:
        thread_lock.release()


def _acquire_workspace_file_lock(lock_file: BinaryIO) -> None:
    """在跨进程锁被占用时有界等待，避免文件写入永久卡住调用者。"""
    lock_file.seek(0, 2)
    if lock_file.tell() == 0:
        lock_file.write(b" ")
        lock_file.flush()
    lock_file.seek(0)
    deadline = time.monotonic() + MUTATION_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            portalocker.lock(
                lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB
            )
        except portalocker.AlreadyLocked as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "文件写入跨进程锁获取超时: "
                    f"timeout_seconds={MUTATION_LOCK_TIMEOUT_SECONDS:g}"
                ) from error
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        else:
            return


def _release_workspace_file_lock(lock_file: BinaryIO) -> None:
    portalocker.unlock(lock_file)
