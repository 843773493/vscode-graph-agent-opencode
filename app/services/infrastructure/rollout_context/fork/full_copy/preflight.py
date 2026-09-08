"""full-copy facade 的格式探测不打开 source SQLite，避免创建 WAL/SHM。"""

from __future__ import annotations

from app.services.infrastructure.rollout_context.fork.cloning import _source_snapshot
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)


def read_source_format(storage, session_id: str, checkpoint_ns: str) -> int:
    source = storage.root(session_id, checkpoint_ns)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("source-mismatch: fork source rollout 不存在或不是普通目录")
    lock = _RolloutFileLock(source.parent / ".rollout.write.lock", exclusive=False)
    lock.acquire()
    try:
        with _source_snapshot(source, storage.sessions_dir) as connection:
            return storage._rollout_format(connection)
    finally:
        lock.release()
