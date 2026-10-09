"""按需加载文件条目的无覆盖原子发布能力。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def rename_entry_exclusive(source: Path, target: Path) -> None:
    if os.name == "nt":
        # Windows rename 对已存在的目标明确失败。
        os.rename(source, target)
        return
    if sys.platform != "linux":
        # TODO：其它平台需实测原生无覆盖 rename 后才开放，不能以检查后覆盖代替。
        raise RuntimeError("当前平台没有已验证的文件条目无覆盖原子发布能力")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
