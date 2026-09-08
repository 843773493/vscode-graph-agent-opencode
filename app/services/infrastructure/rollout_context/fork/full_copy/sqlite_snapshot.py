"""隔离 live SQLite 原始字节复制，不能关闭本进程其它 reader 的 POSIX 锁。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SQLITE_COPY_FILES = ("index.sqlite", "index.sqlite-wal", "index.sqlite-journal")
SQLITE_EXCLUDED_FILES = frozenset((*SQLITE_COPY_FILES, "index.sqlite-shm"))

_COPY_SCRIPT = """
import shutil
import sys
from pathlib import Path

source, destination = (Path(value) for value in sys.argv[1:3])
for name in ("index.sqlite", "index.sqlite-wal", "index.sqlite-journal"):
    path = source / name
    if path.is_symlink():
        raise RuntimeError("source SQLite symlink forbidden")
    if path.exists():
        if not path.is_file():
            raise RuntimeError("source SQLite must be a regular file")
        shutil.copyfile(path, destination / name)
"""


def copy_live_sqlite_snapshot(source: Path, destination: Path) -> None:
    """调用方持有 rollout 共享锁；父进程不 open/close live DB/WAL/journal。

    只复制已发布 source 的权威 SQLite 字节，SHM 在私有副本中重建。
    POSIX record lock 属于进程：必须使用独立解释器，不用线程或父进程
    copyfile/copytree；正文 detail 与密钥不经过该子进程。
    """
    result = subprocess.run(
        [sys.executable, "-I", "-c", _COPY_SCRIPT, str(source), str(destination)],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("source-mismatch: 隔离进程复制 SQLite snapshot 失败")
