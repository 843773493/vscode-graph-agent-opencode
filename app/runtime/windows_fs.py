"""公共文件系统的 Windows 句柄、同步和私有 ACL 边界。"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_PRIVATE_SIDS = ("S-1-3-4", "S-1-5-18", "S-1-5-32-544")


def process_working_directory(directory: str | Path | None) -> str | None:
    """CreateProcess 的 cwd 仍受 MAX_PATH 限制；短名只改变同一目录的原生表示。"""
    if directory is None:
        return None
    value = os.path.abspath(directory)
    if len(value.encode("utf-16-le")) < 520:
        return value
    import win32api

    with _native_errors(Path(value)):
        shortened = win32api.GetShortPathName(value)
    if len(shortened.encode("utf-16-le")) >= 520:
        raise OSError(errno.ENAMETOOLONG, "Windows 进程工作目录超出 MAX_PATH，且没有可用的短路径", value)
    return shortened


@contextmanager
def _native_errors(path: Path | None = None) -> Iterator[None]:
    """保持公共文件系统的 OSError 子类和 Windows 错误码合同。"""
    import pywintypes

    try:
        yield
    except pywintypes.error as error:
        raise OSError(error.winerror, error.strerror, str(path) if path is not None else None, error.winerror) from error


def file_identity(descriptor: int) -> tuple[str, str]:
    """以原生卷序列号和文件索引核验持有的文件，与 Node fstat 共用同一表示。"""
    if sys.platform != "win32":
        raise OSError("Windows 文件身份入口只能在 Windows 调用")
    import msvcrt

    import win32file

    with _native_errors():
        information = win32file.GetFileInformationByHandle(msvcrt.get_osfhandle(descriptor))
    return str(information[4]), str((information[8] << 32) | information[9])


def flush_directory(directory: Path) -> None:
    import win32con
    import win32file

    with _native_errors(directory):
        handle = win32file.CreateFile(
            str(directory),
            win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        try:
            win32file.FlushFileBuffers(int(handle))
        finally:
            handle.Close()


def rename_entry_exclusive(source: Path, target: Path) -> None:
    import win32file

    with _native_errors(target):
        # 仅同步发布，不允许覆盖或跨卷 copy/delete。
        win32file.MoveFileEx(str(source), str(target), win32file.MOVEFILE_WRITE_THROUGH)


def entry_name(path: Path) -> str:
    """取原逻辑目录项拼写；不解析链接或按文件 ID 合并入口。"""
    import win32file

    # 先让真实 I/O 核验目录自身的大小写规则；FindFilesW 的匹配不能代替存在性检查。
    path.lstat()
    with _native_errors(path):
        entries = win32file.FindFilesW(str(path))
    exact = [entry[8] for entry in entries if entry[8] == path.name]
    if exact:
        return exact[0]
    if len(entries) != 1:
        raise OSError(f"文件目录项的规范拼写不唯一: {path}")
    return entries[0][8]


def private_acl_is_current(path: Path, *, is_directory: bool) -> bool:
    import ntsecuritycon
    import win32con
    import win32security

    with _native_errors(path):
        descriptor = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION,
        )
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None or acl.GetAceCount() != len(_PRIVATE_SIDS):
            return False
        if is_directory:
            control, _revision = descriptor.GetSecurityDescriptorControl()
            if not control & win32security.SE_DACL_PROTECTED:
                return False
        actual: dict[str, int] = {}
        inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        for index in range(acl.GetAceCount()):
            (kind, flags), mask, sid = acl.GetAce(index)
            if kind != win32security.ACCESS_ALLOWED_ACE_TYPE or flags & win32con.INHERIT_ONLY_ACE:
                return False
            if is_directory and (flags & inheritance != inheritance or flags & win32con.NO_PROPAGATE_INHERIT_ACE):
                return False
            actual[win32security.ConvertSidToStringSid(sid)] = mask
        return actual == dict.fromkeys(_PRIVATE_SIDS, ntsecuritycon.FILE_ALL_ACCESS)


def set_private_acl(path: Path, *, is_directory: bool) -> None:
    import ntsecuritycon
    import win32con
    import win32security

    inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE if is_directory else 0
    with _native_errors(path):
        acl = win32security.ACL()
        for value in _PRIVATE_SIDS:
            acl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION, inheritance, ntsecuritycon.FILE_ALL_ACCESS,
                win32security.ConvertStringSidToSid(value),
            )
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None,
        )
