"""隔离 live SQLite 原始字节复制，不能关闭本进程其它 reader 的 POSIX 锁。"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.atomic_fs import native_io_path
from app.core.sqlite_state import sqlite_file_uri, validate_sqlite_path_budget
from app.services.infrastructure.rollout_context.storage.primitives import (
    _storage_failure,
    _storage_io,
)

logger = logging.getLogger(__name__)

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

def manifest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()

request = json.loads(sys.stdin.buffer.read())
source, destination, private = (Path(request[key]) for key in ('source', 'destination', 'private'))
rollout_lock_held = request['rollout_lock_held']
files = []
for entry in request['files']:
    name = entry['name']
    path = source / name
    if path.is_symlink():
        raise RuntimeError("source-mismatch: source SQLite 禁止符号链接")
    if path.exists():
        if not path.is_file():
            raise RuntimeError("source-mismatch: source SQLite 必须是普通文件")
        if rollout_lock_held:
            # rollout 共享锁冻结写入，原页/WAL 只在隔离进程内复制。
            # SQLite 只打开私有副本，不能改 live source 的 WAL-index/read mark。
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
        with closing(sqlite3.connect(entry['origin_uri'] + '?mode=ro', uri=True)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            with closing(sqlite3.connect(entry['target_uri'], uri=True)) as writer:
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
    with _storage_io(destination, "source-mismatch: SQLite snapshot 私有目录创建失败", "sqlite_private_create", artifact="fork_source"):
        directory = TemporaryDirectory(prefix=".sqlite-copy-", dir=native_io_path(destination))
    private = directory.name
    try:
        entries = []
        for name in filenames:
            if not name.endswith(".sqlite"):
                continue
            origin = Path(private) / name if rollout_lock_held else source / name
            target = destination / name
            validate_sqlite_path_budget(origin)
            validate_sqlite_path_budget(target)
            entries.append({"name": name, "origin_uri": sqlite_file_uri(origin), "target_uri": sqlite_file_uri(target)})
        request = {
            "source": str(native_io_path(source)), "destination": str(native_io_path(destination)),
            "private": private, "rollout_lock_held": rollout_lock_held, "files": entries,
        }
        command = [sys.executable, "-I", "-c", _COPY_SCRIPT]
        try:
            with _storage_io(destination, "source-mismatch: SQLite snapshot worker 启动失败", "sqlite_worker", artifact="fork_source"):
                result = subprocess.run(
                    command,
                    input=json.dumps(request).encode("utf-8"),
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
        except subprocess.TimeoutExpired as error:
            if error.stderr is not None:
                logger.error("SQLite snapshot worker 超时原始 stderr: %s", error.stderr.decode(errors="replace"))
            detail = _storage_failure(
                destination, "source-mismatch: SQLite snapshot worker 超时", "sqlite_timeout", error, artifact="fork_source",
            )
            raise subprocess.TimeoutExpired(detail, error.timeout) from error
        if result.returncode != 0:
            error = subprocess.CalledProcessError(result.returncode, command, output=result.stdout, stderr=result.stderr)
            logger.error("SQLite snapshot worker 失败原始 stderr: %s", result.stderr.decode(errors="replace"))
            raise RuntimeError(_storage_failure(
                destination, f"source-mismatch: 隔离进程复制 SQLite snapshot 失败 returncode={result.returncode}",
                "sqlite_worker", error, artifact="fork_source",
            )) from error
    finally:
        with _storage_io(Path(private), "source-mismatch: SQLite snapshot 私有目录清理失败", "sqlite_private_cleanup", artifact="fork_source"):
            directory.cleanup()
    return tuple((name, size, digest) for name, size, digest in json.loads(result.stdout))
