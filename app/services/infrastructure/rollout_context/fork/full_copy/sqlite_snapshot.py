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
import sys
from pathlib import Path

def manifest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()

source, destination = (Path(value) for value in sys.argv[1:3])
files = []
for name in sys.argv[3:]:
    path = source / name
    if path.is_symlink():
        raise RuntimeError("source-mismatch: source SQLite 禁止符号链接")
    if path.exists():
        if not path.is_file():
            raise RuntimeError("source-mismatch: source SQLite 必须是普通文件")
        source_fact = manifest(path)
        target = destination / name
        shutil.copyfile(path, target)
        if manifest(target) != source_fact:
            raise RuntimeError("source-mismatch: SQLite snapshot 与冻结 source 字节不一致")
        files.append((name, *source_fact))
print(json.dumps(files))
"""


def copy_live_sqlite_snapshot(
    source: Path, destination: Path, *, filenames: tuple[str, ...] = SQLITE_COPY_FILES,
) -> tuple[tuple[str, int, str], ...]:
    """调用方持有 rollout 共享锁；父进程不 open/close live DB/WAL/journal。

    只复制已发布 source 的权威 SQLite 字节，SHM 在私有副本中重建。
    POSIX record lock 属于进程：必须使用独立解释器，不用线程或父进程
    copyfile/copytree；正文 detail 与密钥不经过该子进程。
    源 manifest 与复制校验也在子进程完成，返回源文件名、长度和 SHA。
    """
    result = subprocess.run(
        [sys.executable, "-I", "-c", _COPY_SCRIPT, str(source), str(destination), *filenames],
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
