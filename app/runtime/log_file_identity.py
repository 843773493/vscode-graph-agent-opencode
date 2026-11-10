"""不同运行时对日志文件描述符和路径使用同一文件身份表示。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def runtime_log_file_identity(descriptor: int) -> tuple[str, str]:
    """按 Gateway 日志 owner 的平台合同读取实际文件句柄身份。"""
    if sys.platform == "win32":
        from app.runtime.windows_fs import file_identity

        return file_identity(descriptor)
    identity = os.fstat(descriptor)
    return str(identity.st_dev), str(identity.st_ino)


def runtime_log_path_identity(path: Path) -> tuple[str, str]:
    """打开当前路径后读取句柄身份，不使用不一致的平台路径 stat 表示。"""
    with path.open("rb") as stream:
        return runtime_log_file_identity(stream.fileno())
