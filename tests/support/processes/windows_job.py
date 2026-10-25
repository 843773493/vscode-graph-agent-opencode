"""在 Windows Job Object 中运行命令，并在根进程退出时回收全部后代。"""

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

# TODO: 在真实 Windows VMware 上验证矩阵 runner 和远端目标入口的 pywin32 Job 生命周期。


class _CompletionPort(ctypes.Structure):
    _fields_ = [("key", ctypes.c_void_p), ("port", wintypes.HANDLE)]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.SetInformationJobObject.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
)
_kernel.SetInformationJobObject.restype = wintypes.BOOL
_kernel.GetQueuedCompletionStatus.argtypes = (
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.DWORD,
)
_kernel.GetQueuedCompletionStatus.restype = wintypes.BOOL


def _next_notification(port: object, wait_ms: int) -> tuple[int, int]:
    message, key, process_id = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
    # Job通知的末项是PID，不是PyOVERLAPPED，不能由IOCP包装器解引用。
    succeeded = _kernel.GetQueuedCompletionStatus(
        int(port),
        ctypes.byref(message),
        ctypes.byref(key),
        ctypes.byref(process_id),
        wait_ms,
    )
    error = 0 if succeeded else ctypes.get_last_error()
    if error not in (0, win32event.WAIT_TIMEOUT):
        raise ctypes.WinError(error)
    return error, message.value


def _stop_job(job: object, process: object) -> None:
    if (
        not win32job.IsProcessInJob(process, job)
        and win32event.WaitForSingleObject(process, 0) == win32event.WAIT_TIMEOUT
    ):
        win32api.TerminateProcess(process, 1)
    active_processes = win32job.QueryInformationJobObject(
        job, win32job.JobObjectBasicAccountingInformation
    )["ActiveProcesses"]
    if active_processes:
        win32job.TerminateJobObject(job, 1)
    if win32event.WaitForSingleObject(process, 10_000) != win32event.WAIT_OBJECT_0:
        raise RuntimeError("Windows 受管进程根进程未退出")
    deadline = time.monotonic() + 10
    while win32job.QueryInformationJobObject(
        job, win32job.JobObjectBasicAccountingInformation
    )["ActiveProcesses"]:
        if time.monotonic() >= deadline:
            raise RuntimeError("Windows Job Object 后代未全部退出")
        time.sleep(0.01)


def run_windows_job(
    command: list[str],
    *,
    memory_bytes: int,
    timeout_seconds: float | None = None,
    timeout_message: str = "Windows 受管进程树超时",
    memory_limit_message: str = "Windows 受管进程树内存超限",
) -> int:
    if not command or memory_bytes <= 0:
        raise ValueError("Windows Job Object 需要非空命令和正内存上限")
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    with ExitStack() as handles:
        job = win32job.CreateJobObject(None, "")
        handles.callback(job.Close)
        limits = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        limits["JobMemoryLimit"] = memory_bytes
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, limits
        )

        port = win32file.CreateIoCompletionPort(
            win32file.INVALID_HANDLE_VALUE, None, 0, 1
        )
        handles.callback(port.Close)
        association = _CompletionPort(1, int(port))
        if not _kernel.SetInformationJobObject(
            int(job),
            win32job.JobObjectAssociateCompletionPortInformation,
            ctypes.byref(association),
            ctypes.sizeof(association),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        process, thread, _, _ = win32process.CreateProcess(
            None,
            subprocess.list2cmdline(command),
            None,
            None,
            True,
            win32con.CREATE_SUSPENDED,
            None,
            None,
            win32process.STARTUPINFO(),
        )
        handles.callback(process.Close)
        handles.callback(thread.Close)
        handles.callback(_stop_job, job, process)
        win32job.AssignProcessToJobObject(job, process)
        win32process.ResumeThread(thread)

        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(timeout_message, file=sys.stderr, flush=True)
                    return 124
                wait_ms = min(100, max(1, int(remaining * 1000)))
            else:
                wait_ms = 100
            error, message = _next_notification(port, wait_ms)
            if message == win32job.JOB_OBJECT_MSG_JOB_MEMORY_LIMIT:
                print(memory_limit_message, file=sys.stderr, flush=True)
                return 137
            if (
                error == win32event.WAIT_TIMEOUT
                and win32event.WaitForSingleObject(process, 0)
                == win32event.WAIT_OBJECT_0
            ):
                return win32process.GetExitCodeProcess(process)


def main(arguments: list[str]) -> int:
    try:
        separator = arguments.index("--")
    except ValueError as error:
        raise ValueError("Windows Job Object runner 缺少命令分隔符 --") from error
    if (
        separator != 2
        or arguments[0] != "--memory-limit-kb"
        or not arguments[1].isdecimal()
    ):
        raise ValueError("Windows Job Object runner 参数无效")
    memory_limit_kb = int(arguments[1])
    command = arguments[separator + 1 :]
    if memory_limit_kb <= 0 or not command:
        raise ValueError("Windows Job Object runner 需要正内存上限和非空命令")
    return run_windows_job(command, memory_bytes=memory_limit_kb * 1024)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
