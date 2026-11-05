"""文件级 durability 原语：目录/文件 fsync 与原子写。

会话创建、child thread 创建、catalog 迁移与子树删除四条链路共享同一套
崩溃安全语义（tempfile + fsync 文件 + os.replace + fsync 目录项），此处
单点承载，避免同一实现散落多处后漂移。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.runtime import windows_fs

__all__ = ["atomic_write_bytes", "fsync_directory", "fsync_file", "native_io_path"]


def native_io_path(path: Path) -> Path:
    """只在原生 I/O 边界扩展 Windows 路径，不用于逻辑归属或 URI。"""
    if os.name != "nt":
        return path
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return Path(value)
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


def fsync_directory(directory: Path) -> None:
    """请求目标文件系统同步目录元数据；不支持时明确抛错。"""
    if os.name == "nt":
        windows_fs.flush_directory(native_io_path(directory))
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    """fsync 已存在文件（durability barrier 组成部分）。"""
    # Windows CRT 的 _commit 需要可写描述符；只读描述符会直接返回 EBADF。
    descriptor = os.open(native_io_path(path), os.O_RDWR if os.name == "nt" else os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """tempfile + fsync + os.replace 的原子写，并 fsync 目标目录项。"""
    path = native_io_path(path)
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
