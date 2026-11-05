"""公共文件系统的 Windows 句柄、同步和私有 ACL 边界。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_PRIVATE_SIDS = ("S-1-3-4", "S-1-5-18", "S-1-5-32-544")


@contextmanager
def _native_errors(path: Path) -> Iterator[None]:
    """保持公共文件系统的 OSError 子类和 Windows 错误码合同。"""
    import pywintypes

    try:
        yield
    except pywintypes.error as error:
        raise OSError(error.winerror, error.strerror, str(path), error.winerror) from error


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
