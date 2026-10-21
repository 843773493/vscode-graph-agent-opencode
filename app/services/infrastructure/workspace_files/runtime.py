"""按需加载文件条目的无覆盖原子发布能力。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 4


def rename_entry_exclusive(source: Path, target: Path) -> None:
    if os.name == "nt":
        # Windows rename 对已存在的目标明确失败。
        os.rename(source, target)
        return
    if sys.platform not in ("linux", "darwin"):
        raise RuntimeError("当前平台没有已验证的文件条目无覆盖原子发布能力")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux":
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
    else:
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(target), _RENAME_EXCL)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
