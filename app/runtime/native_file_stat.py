"""按原 Node/libuv 的创建时间语义读取平台文件元数据。"""

from __future__ import annotations

import errno
import math
import os
import sys
from pathlib import Path


def date_milliseconds(timestamp_ns: int) -> float:
    """固定 Node Stats 的 Date getter 使用 MathRound，影响原 FileService 的 etag。"""
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return float(math.floor(seconds * 1000 + nanoseconds / 1_000_000 + 0.5))


def birthtime_milliseconds(path: Path, metadata: os.stat_result, *, follow_symlinks: bool) -> float:
    if sys.platform == "win32":
        # TODO：最低 Python 升至 3.12 后删除此分支；3.11 的 Windows ctime 仍是创建时间。
        timestamp = getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns)
        return date_milliseconds(timestamp)
    if sys.platform != "linux":
        raise NotImplementedError("原生扩展文件元数据当前仅验证 Linux 和 Windows")

    import ctypes

    class Timestamp(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_int64), ("nanoseconds", ctypes.c_uint32), ("reserved", ctypes.c_int32)]

    class Statx(ctypes.Structure):
        # Linux statx 的固定 256 字节 ABI；只投影出生时间，其余字段不形成第二文件 API。
        _fields_ = [("header", ctypes.c_byte * 64), ("atime", Timestamp), ("btime", Timestamp),
                    ("remaining", ctypes.c_byte * 160)]

    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "statx"):
        raise NotImplementedError("当前 Linux C runtime 未提供 statx，无法核实原生文件创建时间")
    query = libc.statx
    query.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Statx)]
    query.restype = ctypes.c_int
    result = Statx()
    status = query(-100, os.fsencode(path), 0 if follow_symlinks else 0x100, 0xFFF, ctypes.byref(result))
    if status != 0:
        error = ctypes.get_errno()
        # TODO：移除旧内核支持时同步核固定 libuv；这些 errno 在原实现中明确走传统 stat 的 ctime。
        if error in {errno.ENOSYS, errno.EINVAL, errno.EPERM, errno.EOPNOTSUPP}:
            return date_milliseconds(metadata.st_ctime_ns)
        raise OSError(error, os.strerror(error), str(path))
    # libuv 对成功 statx 直接读取零初始化 btime；文件系统未提供出生时间时保留其 0。
    return date_milliseconds(result.btime.seconds * 1_000_000_000 + result.btime.nanoseconds)
