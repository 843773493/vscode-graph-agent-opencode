"""按需加载文件条目的无覆盖原子发布能力。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.core.atomic_fs import native_io_path

_DARWIN_RENAME_EXCL = 0x00000004


def rename_entry_exclusive(source: Path, target: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        # WRITE_THROUGH；不允许覆盖或跨卷 copy/delete 回退。
        if not move(str(native_io_path(source)), str(native_io_path(target)), 8):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    if sys.platform == "darwin":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(
            os.fsencode(source),
            os.fsencode(target),
            _DARWIN_RENAME_EXCL,
        ) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
        return
    if sys.platform != "linux":
        raise RuntimeError("当前平台没有已验证的文件条目无覆盖原子发布能力")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
