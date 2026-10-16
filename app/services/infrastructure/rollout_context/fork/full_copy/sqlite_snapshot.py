"""隔离 live SQLite 原始字节复制，不能关闭本进程其它 reader 的 POSIX 锁。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SQLITE_COPY_FILES = ("index.sqlite", "index.sqlite-wal", "index.sqlite-journal")
SQLITE_EXCLUDED_FILES = frozenset((*SQLITE_COPY_FILES, "index.sqlite-shm"))

_COPY_SCRIPT = """
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

def manifest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()

source, destination = (Path(value) for value in sys.argv[1:3])
rollout_lock_held = sys.argv[3] == '1'
files = []
with TemporaryDirectory(prefix='.sqlite-copy-', dir=destination) as private:
    for name in sys.argv[4:]:
        if not name.endswith('.sqlite'):
            continue
        path = source / name
        if path.is_symlink():
            raise RuntimeError("source-mismatch: source SQLite 禁止符号链接")
        if path.exists():
            if not path.is_file():
                raise RuntimeError("source-mismatch: source SQLite 必须是普通文件")
            origin = path
            if rollout_lock_held:
                # rollout 共享锁冻结写入，原页/WAL 只在隔离进程内复制。
                # SQLite 只打开私有副本，不能改 live source 的 WAL-index/read mark。
                origin = Path(private) / name
                for suffix in ('', '-wal', '-journal'):
                    original = source / (name + suffix)
                    if original.is_symlink():
                        raise RuntimeError("source-mismatch: source SQLite 禁止符号链接")
                    if original.exists():
                        if not original.is_file():
                            raise RuntimeError("source-mismatch: source SQLite 必须是普通文件")
                        shutil.copyfile(original, Path(private) / (name + suffix))
            # session-control 没有 rollout 写排他锁，直接保持固定 online transaction。
            target = destination / name
            with closing(sqlite3.connect(origin.as_uri() + '?mode=ro', uri=True)) as reader:
                reader.execute('BEGIN')
                reader.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
                with closing(sqlite3.connect(target)) as writer:
                    reader.backup(writer)
                    writer.execute('PRAGMA journal_mode=DELETE')
                reader.rollback()
            files.append((name, *manifest(target)))
print(json.dumps(files))
"""


def copy_live_sqlite_snapshot(
    source: Path, destination: Path, *, filenames: tuple[str, ...] = SQLITE_COPY_FILES,
    rollout_lock_held: bool = False,
) -> tuple[tuple[str, int, str], ...]:
    """隔离进程以固定 read transaction 冻结 committed SQLite pages。

    rollout 调用方持共享锁，隔离进程先私有复制原页/WAL，避免改 source SHM；
    session-control 保持 online reader。输出只含 committed 页，不保留 WAL/journal 尾部。
    POSIX record lock 属于进程：必须使用独立解释器，不用线程或父进程
    copyfile/copytree；正文 detail 与密钥不经过该子进程。
    源 manifest 与复制校验也在子进程完成，返回源文件名、长度和 SHA。
    """
    result = subprocess.run(
        [sys.executable, "-I", "-c", _COPY_SCRIPT, str(source), str(destination),
         "1" if rollout_lock_held else "0", *filenames],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "source-mismatch: 隔离进程复制 SQLite snapshot 失败: "
            + result.stderr.decode(errors="replace").strip()
        )
    return tuple((name, size, digest) for name, size, digest in json.loads(result.stdout))
