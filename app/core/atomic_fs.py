"""文件级 durability 原语：目录/文件 fsync 与原子写。

会话创建、child thread 创建、catalog 迁移与子树删除四条链路共享同一套
崩溃安全语义（tempfile + fsync 文件 + os.replace + fsync 目录项），此处
单点承载，避免同一实现散落多处后漂移。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["atomic_write_bytes", "fsync_directory", "fsync_file"]


def fsync_directory(directory: Path) -> None:
    """fsync 目录项，保证新建/改名条目的持久性。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    """fsync 已存在文件（durability barrier 组成部分）。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """tempfile + fsync + os.replace 的原子写，并 fsync 目标目录项。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    fsync_directory(path.parent)
