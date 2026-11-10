"""Linux 原生进程描述符，不依赖 Python 构建机是否暴露 pidfd_open。"""

from __future__ import annotations

import os


def open_process_descriptor(pid: int) -> int:
    import ctypes

    # 受支持的 Linux x64／arm64 使用同一系统调用号。固定 Python 以旧 glibc
    # 构建时不会暴露 os.pidfd_open；直接向内核取得同一个可等待描述符。
    syscall = ctypes.CDLL(None, use_errno=True).syscall
    syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_uint]
    syscall.restype = ctypes.c_long
    descriptor = syscall(434, pid, 0)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return descriptor
