"""跨平台私有权限、只读保护与指定目录的只读清理原语。"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.atomic_fs import native_io_path
from app.runtime import windows_fs


def _is_windows() -> bool:
    return os.name == "nt"


def restrict_private_path_access(
    path: Path,
    *,
    is_directory: bool = False,
    descriptor: int | None = None,
) -> None:
    """只允许当前所有者、SYSTEM 和本机管理员访问目标。"""
    path = native_io_path(path)
    if _is_windows():
        if not windows_fs.private_acl_is_current(path, is_directory=is_directory):
            windows_fs.set_private_acl(path, is_directory=is_directory)
        return
    mode = 0o700 if is_directory else 0o600
    if descriptor is None:
        path.chmod(mode)
    else:
        os.fchmod(descriptor, mode)


def require_private_file_access(
    path: Path,
    *,
    descriptor: int | None = None,
) -> None:
    """读取私有凭据前确认权限；Windows 直接查询原生 DACL。"""
    if _is_windows():
        restrict_private_path_access(path)
        return

    file_stat = os.fstat(descriptor) if descriptor is not None else path.stat()
    if file_stat.st_mode & 0o077:
        raise PermissionError(f"私有文件权限必须仅对所有者开放: {path}")


def protect_readonly_file(path: Path) -> None:
    """同一文件句柄内设置只读并同步；Windows 重试先撤下只读位。"""
    path = native_io_path(path)
    if _is_windows():
        path.chmod(0o600)
    with path.open("r+b" if _is_windows() else "rb") as stream:
        path.chmod(0o400)
        os.fsync(stream.fileno())


def prepare_readonly_directory_removal(directory: Path) -> None:
    """仅恢复指定目录及其直接普通文件的写位，不递归提权或跟随链接。"""
    directory = native_io_path(directory)
    if directory.is_symlink():
        raise RuntimeError("只读目录清理不能经过符号链接")
    if directory.is_dir():
        directory.chmod(0o700)
        for path in directory.iterdir():
            if not path.is_symlink() and path.is_file():
                path.chmod(0o600)
