"""用 Windows Job Object 在进程外限制测试并回收全部后代。"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from contextlib import ExitStack
from ctypes import wintypes

import win32api
import win32con
import win32event
import win32file
import win32job
import win32process

TIMEOUT_SECONDS = 600
MEMORY_BYTES = 4 * 1024**3


class _CompletionPort(ctypes.Structure):
    _fields_ = [("key", ctypes.c_void_p), ("port", wintypes.HANDLE)]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
_kernel.SetInformationJobObject.restype = wintypes.BOOL
_kernel.GetQueuedCompletionStatus.argtypes = (
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
)
_kernel.GetQueuedCompletionStatus.restype = wintypes.BOOL


def _next_notification(port, wait_ms: int) -> tuple[int, int]:
    message, key, pid = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
    # Job 通知的末项是 PID，不是 PyOVERLAPPED，不能让通用 IOCP 包装器解引用它。
    ok = _kernel.GetQueuedCompletionStatus(int(port), ctypes.byref(message), ctypes.byref(key), ctypes.byref(pid), wait_ms)
    error = 0 if ok else ctypes.get_last_error()
    if error not in (0, win32event.WAIT_TIMEOUT):
        raise ctypes.WinError(error)
    return error, message.value


def _stop_job(job, process) -> None:
    # 绑定失败时根进程仍处于挂起状态；已绑定者只由 Job 终止，避免退出竞态。
    if not win32job.IsProcessInJob(process, job) and win32event.WaitForSingleObject(process, 0) == win32event.WAIT_TIMEOUT:
        win32api.TerminateProcess(process, 1)
    win32job.TerminateJobObject(job, 1)
    if win32event.WaitForSingleObject(process, 10_000) != win32event.WAIT_OBJECT_0:
        raise RuntimeError("Windows 测试根进程未退出")
    deadline = time.monotonic() + 10
    while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
        if time.monotonic() >= deadline:
            raise RuntimeError("Windows 测试 Job 后代未全部退出")
        time.sleep(0.01)


def run_command(command: list[str], *, timeout_seconds: float = TIMEOUT_SECONDS, memory_bytes: int = MEMORY_BYTES) -> int:
    with ExitStack() as handles:
        job = win32job.CreateJobObject(None, "")
        handles.callback(job.Close)
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        limits["JobMemoryLimit"] = memory_bytes
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        port = win32file.CreateIoCompletionPort(win32file.INVALID_HANDLE_VALUE, None, 0, 1)
        handles.callback(port.Close)
        association = _CompletionPort(1, int(port))
        if not _kernel.SetInformationJobObject(
            int(job), win32job.JobObjectAssociateCompletionPortInformation,
            ctypes.byref(association), ctypes.sizeof(association),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # 先挂起创建并绑定 Job，再运行用户代码，消除启动期间生成未受控后代的窗口。
        process, thread, _, _ = win32process.CreateProcess(
            None, subprocess.list2cmdline(command), None, None, True,
            win32con.CREATE_SUSPENDED, None, None, win32process.STARTUPINFO(),
        )
        handles.callback(process.Close)
        handles.callback(thread.Close)
        handles.callback(_stop_job, job, process)
        win32job.AssignProcessToJobObject(job, process)
        deadline = time.monotonic() + timeout_seconds
        win32process.ResumeThread(thread)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"Windows 测试超时: {timeout_seconds}s", file=sys.stderr, flush=True)
                return 124
            error, message = _next_notification(port, min(100, max(1, int(remaining * 1000))))
            if message == win32job.JOB_OBJECT_MSG_JOB_MEMORY_LIMIT:
                print(f"Windows 测试进程树内存超限: limit={memory_bytes}", file=sys.stderr, flush=True)
                return 137
            # 消费完已排队的内存通知后才接受正常退出，避免把分配失败误记为普通失败。
            if error == win32event.WAIT_TIMEOUT and win32event.WaitForSingleObject(process, 0) == win32event.WAIT_OBJECT_0:
                return win32process.GetExitCodeProcess(process)


if __name__ == "__main__":
    raise SystemExit(run_command([sys.executable, "-m", "pytest", *sys.argv[1:]]))
