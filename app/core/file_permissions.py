"""跨平台私有文件和目录权限原语。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_WINDOWS_FILE_ALL_ACCESS = 0x001F01FF
_WINDOWS_OBJECT_INHERIT_ACE = 0x01
_WINDOWS_CONTAINER_INHERIT_ACE = 0x02
_WINDOWS_NO_PROPAGATE_INHERIT_ACE = 0x04
_WINDOWS_INHERIT_ONLY_ACE = 0x08


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_acl_is_private(path: Path, *, is_directory: bool) -> bool:
    """用原生 DACL 确认目录已关闭继承且只含预期主体，避免反复启动 icacls。"""
    import pywintypes
    import win32security

    try:
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
    except pywintypes.error:
        return False

    access_control_list = descriptor.GetSecurityDescriptorDacl()
    if access_control_list is None or access_control_list.GetAceCount() != 3:
        return False
    if is_directory:
        control, _revision = descriptor.GetSecurityDescriptorControl()
        if (control & win32security.SE_DACL_PROTECTED) == 0:
            return False

    expected_sids = {"S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
    actual_access: dict[str, int] = {}
    for index in range(access_control_list.GetAceCount()):
        header, access_mask, sid = access_control_list.GetAce(index)
        if header[0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
            return False
        ace_flags = header[1]
        if ace_flags & _WINDOWS_INHERIT_ONLY_ACE:
            return False
        if is_directory:
            inheritance = _WINDOWS_OBJECT_INHERIT_ACE | _WINDOWS_CONTAINER_INHERIT_ACE
            if ace_flags & inheritance != inheritance:
                return False
            if ace_flags & _WINDOWS_NO_PROPAGATE_INHERIT_ACE:
                return False
        actual_access[win32security.ConvertSidToStringSid(sid)] = access_mask

    return actual_access == {sid: _WINDOWS_FILE_ALL_ACCESS for sid in expected_sids}


def restrict_private_path_access(
    path: Path,
    *,
    is_directory: bool = False,
    descriptor: int | None = None,
) -> None:
    """只允许当前所有者、SYSTEM 和本机管理员访问目标。"""
    if not _is_windows():
        mode = 0o700 if is_directory else 0o600
        if descriptor is None:
            path.chmod(mode)
        else:
            os.fchmod(descriptor, mode)
        return

    if _windows_acl_is_private(path, is_directory=is_directory):
        return

    inheritance = ":(OI)(CI)F" if is_directory else ":F"
    commands = (
        ["icacls", str(path), "/reset"],
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*S-1-3-4{inheritance}",
            f"*S-1-5-18{inheritance}",
            f"*S-1-5-32-544{inheritance}",
        ],
    )
    for arguments in commands:
        try:
            result = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(
                f"无法设置私有文件 ACL: path={path}, command={arguments}, error={error}"
            ) from error
        if result.returncode != 0:
            raise PermissionError(
                "icacls 拒绝限制私有文件访问: "
                f"path={path}, command={arguments}, "
                f"exit_code={result.returncode}, "
                f"stdout={result.stdout.strip()}, stderr={result.stderr.strip()}"
            )


def require_private_file_access(
    path: Path,
    *,
    descriptor: int | None = None,
) -> None:
    """读取私有凭据前确保访问边界受限；Windows 合规时只查询 DACL，不启动子进程。"""
    if _is_windows():
        restrict_private_path_access(path)
        return

    file_stat = os.fstat(descriptor) if descriptor is not None else path.stat()
    if file_stat.st_mode & 0o077:
        raise PermissionError(f"私有文件权限必须仅对所有者开放: {path}")
