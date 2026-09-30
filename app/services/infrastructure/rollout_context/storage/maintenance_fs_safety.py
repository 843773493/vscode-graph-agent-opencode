"""rollout maintenance 的文件系统安全校验（只依赖参数，无 self 状态）。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

__all__ = ["RolloutMaintenanceFsSafetyMixin"]


class RolloutMaintenanceFsSafetyMixin:
    @staticmethod
    def _require_regular_file(path: Path, *, field: str) -> None:
        if path.is_symlink():
            raise RuntimeError(f"{field} 不能是符号链接: {path}")
        if path.exists() and not path.is_file():
            raise RuntimeError(f"{field} 必须是普通文件: {path}")

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _copy_file_fsync(source: Path, target: Path) -> None:
        with source.open("rb") as source_stream, target.open("wb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream)
            target_stream.flush()
            os.fsync(target_stream.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _require_plain_directory(path: Path, *, field: str) -> None:
        if path.is_symlink():
            raise RuntimeError(f"{field} 不能是符号链接: {path}")
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"{field} 必须是普通目录: {path}")

    @staticmethod
    def _require_offline_restore_target(target_path: Path) -> None:
        try:
            with closing(
                sqlite3.connect(
                    f"{target_path.as_uri()}?mode=ro&cache=private",
                    uri=True,
                    timeout=0,
                )
            ) as target:
                target.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.DatabaseError:
            return
        raise RuntimeError(
            "SQLite offline restore 只允许恢复无法打开的损坏 target；"
            "可打开的 target 请使用 restore_index_backup"
        )
