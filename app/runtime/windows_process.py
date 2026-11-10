"""Windows 直接子进程：挂入原 Job Object 后才恢复主线程。"""

from __future__ import annotations

import asyncio
import ntpath
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, TextIO, cast

from anyio.abc import Process
from anyio.streams.file import FileReadStream, FileWriteStream

from app.runtime.windows_fs import process_working_directory


class _NativeHandle(Protocol):
    def __int__(self) -> int: ...

    def Close(self) -> None: ...


class WindowsChildProcess(Process):
    def __init__(self, *, process: _NativeHandle, job: _NativeHandle, pid: int,
                 stdin: FileWriteStream, stdout: FileReadStream | None) -> None:
        self._process, self._job, self._pid = process, job, pid
        self._stdin, self._stdout = stdin, stdout
        self._returncode: int | None = None

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def stdin(self) -> FileWriteStream:
        return self._stdin

    @property
    def stdout(self) -> FileReadStream | None:
        return self._stdout

    @property
    def stderr(self) -> None:
        return None

    @property
    def returncode(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        import win32event
        import win32process

        if win32event.WaitForSingleObject(int(self._process), 0) == win32event.WAIT_TIMEOUT:
            return None
        self._returncode = win32process.GetExitCodeProcess(int(self._process))
        return self._returncode

    def group_empty(self) -> bool:
        import win32job

        return win32job.QueryInformationJobObject(int(self._job), win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"] == 0

    async def wait(self) -> int:
        while (code := self.returncode) is None:
            await asyncio.sleep(0.02)
        return code

    def terminate(self) -> None:
        import win32job

        win32job.TerminateJobObject(int(self._job), 1)

    def kill(self) -> None:
        self.terminate()

    def send_signal(self, signal: int) -> None:
        if signal not in (9, 15):
            raise ValueError(f"Windows 子进程不支持此信号: {signal}")
        self.terminate()

    async def aclose(self) -> None:
        if self.returncode is None or not self.group_empty():
            raise RuntimeError("Windows 原进程／Job Object 尚未核实退出，不能释放句柄")
        await self._stdin.aclose()
        if self._stdout is not None:
            await self._stdout.aclose()
        self._process.Close()
        self._job.Close()


def open_windows_process(command: Sequence[str], *, env: Mapping[str, str],
                         cwd: str | Path | None, stderr: TextIO | Literal[-2],
                         stdout: BinaryIO | TextIO | Literal[-1] = -1) -> WindowsChildProcess:
    if sys.platform != "win32":
        raise OSError("Windows 子进程入口只能在 Windows 调用")
    import msvcrt

    import win32con
    import win32job
    import win32process

    with ExitStack() as owned:
        # pywin32 返回持有 Close 的 PyHANDLE；其随包 stub 把 CreateJobObject 错标为 None。
        job = cast(_NativeHandle, cast(object, win32job.CreateJobObject(None, "")))
        owned.callback(job.Close)
        limits = win32job.QueryInformationJobObject(int(job), win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(int(job), win32job.JobObjectExtendedLimitInformation, limits)

        def abort_process(process: _NativeHandle) -> None:
            import win32api
            import win32event

            win32api.TerminateProcess(int(process), 1)
            if win32event.WaitForSingleObject(int(process), 10_000) != win32event.WAIT_OBJECT_0:
                raise RuntimeError("Windows 创建失败后的原子进程未退出")
            process.Close()

        with ExitStack() as inherited:
            stdin_read, stdin_write = os.pipe()
            child_input = inherited.enter_context(os.fdopen(stdin_read, "rb", buffering=0))
            parent_input = owned.enter_context(os.fdopen(stdin_write, "wb", buffering=0))
            parent_output = None
            if stdout == subprocess.PIPE:
                stdout_read, stdout_write = os.pipe()
                parent_output = owned.enter_context(os.fdopen(stdout_read, "rb", buffering=0))
                child_output = inherited.enter_context(os.fdopen(stdout_write, "wb", buffering=0))
            else:
                child_output = inherited.enter_context(os.fdopen(os.dup(stdout.fileno()), "wb", buffering=0))
            child_error = child_output if stderr == subprocess.STDOUT else inherited.enter_context(
                os.fdopen(os.dup(stderr.fileno()), "wb", buffering=0))
            for stream in (child_input, child_output, child_error):
                os.set_inheritable(stream.fileno(), True)
            startup = win32process.STARTUPINFO()
            startup.dwFlags |= win32con.STARTF_USESTDHANDLES
            startup.hStdInput = msvcrt.get_osfhandle(child_input.fileno())
            startup.hStdOutput = msvcrt.get_osfhandle(child_output.fileno())
            startup.hStdError = msvcrt.get_osfhandle(child_error.fileno())
            # 已解析的绝对 exe 独立传入，避免命令行首 token 的 MAX_PATH 限制；裸命令仍按原 PATH 搜索。
            executable = command[0] if ntpath.isabs(command[0]) else None
            process_handle, thread_handle, pid, _ = win32process.CreateProcess(executable, subprocess.list2cmdline(command),
                None, None, True, win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW, dict(env),
                process_working_directory(cwd), startup)
            process = cast(_NativeHandle, cast(object, process_handle))
            thread = cast(_NativeHandle, cast(object, thread_handle))
            owned.callback(abort_process, process)
            inherited.callback(thread.Close)
            win32job.AssignProcessToJobObject(int(job), int(process))
            win32process.ResumeThread(int(thread))
            result = WindowsChildProcess(process=process, job=job, pid=pid,
                stdin=FileWriteStream(parent_input), stdout=FileReadStream(parent_output) if parent_output is not None else None)
        owned.pop_all()
        return result
