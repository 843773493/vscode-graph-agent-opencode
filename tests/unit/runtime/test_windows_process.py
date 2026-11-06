"""显式替换 Win32 边界，核启动顺序、原句柄等待及创建失败清理。"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from app.runtime import windows_process


@pytest.fixture
def native_process_case(monkeypatch):
    events, descriptors = [], []
    state = SimpleNamespace(code=None, active=1, fail_assign=False, fail_job=False)

    class Handle:
        def __init__(self, name, value):
            self.name, self.value = name, value
            self.closed = False

        def __int__(self):
            if self.closed:
                raise RuntimeError("不能读取已释放的原生句柄")
            return self.value

        def Close(self):
            self.closed = True
            events.append("close_" + self.name)

    job, process, thread = Handle("job", 10), Handle("process", 20), Handle("thread", 30)
    native_pipe = os.pipe

    def pipe():
        result = native_pipe()
        descriptors.extend(result)
        return result

    def create_job(*args):
        if state.fail_job:
            raise OSError("Job 创建失败")
        return job

    def create_process(*args):
        state.application, state.commandline = args[:2]
        assert args[4] is True and args[5] == 12
        assert args[6] == {"MODEL": "configured"}
        startup = args[8]
        assert all(os.get_inheritable(fd) for fd in (startup.hStdInput, startup.hStdOutput, startup.hStdError))
        descriptors.append(startup.hStdError)
        events.append("create_suspended")
        return process, thread, 1234, 5678

    def assign(job_handle, process_handle):
        assert job_handle == int(job) and process_handle == int(process)
        events.append("assign")
        if state.fail_assign:
            raise OSError("原 Job 挂接失败")

    def wait(process_handle, duration):
        assert process_handle == int(process)
        events.append("wait" if duration else "poll")
        return 258 if state.code is None else 0

    def terminate(process_handle, code):
        assert process_handle == int(process)
        events.append("terminate_process")
        state.code, state.active = code, 0

    modules = {
        "msvcrt": SimpleNamespace(get_osfhandle=lambda fd: fd),
        "win32con": SimpleNamespace(STARTF_USESTDHANDLES=1, CREATE_SUSPENDED=4, CREATE_NO_WINDOW=8),
        "win32process": SimpleNamespace(STARTUPINFO=lambda: SimpleNamespace(dwFlags=0), CreateProcess=create_process,
            ResumeThread=lambda handle: events.append("resume"), GetExitCodeProcess=lambda handle: state.code),
        "win32job": SimpleNamespace(CreateJobObject=create_job, JobObjectExtendedLimitInformation=1,
            JobObjectBasicAccountingInformation=2, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=4,
            QueryInformationJobObject=lambda handle, kind: {"BasicLimitInformation": {"LimitFlags": 0}}
                if kind == 1 else {"ActiveProcesses": state.active},
            SetInformationJobObject=lambda handle, kind, limits: events.append("set_limits"),
            AssignProcessToJobObject=assign, TerminateJobObject=lambda handle, code: events.append("terminate_job")),
        "win32event": SimpleNamespace(WaitForSingleObject=wait, WAIT_TIMEOUT=258, WAIT_OBJECT_0=0),
        "win32api": SimpleNamespace(TerminateProcess=terminate),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(windows_process.os, "pipe", pipe)

    def open_process(command=("server.exe", "argument with space")):
        with monkeypatch.context() as platform_patch:
            platform_patch.setattr(windows_process.sys, "platform", "win32")
            return windows_process.open_windows_process(command,
                env={"MODEL": "configured"}, cwd=None, stderr=sys.stderr)

    return SimpleNamespace(open=open_process, state=state, events=events, descriptors=descriptors)


async def test_windows_creation_assigns_job_before_resume_and_keeps_original_handles(native_process_case):
    case = native_process_case
    process = case.open()
    assert case.events == ["set_limits", "create_suspended", "assign", "resume", "close_thread"]
    assert process.pid == 1234 and process.returncode is None and not process.group_empty()
    process.kill()
    assert case.events[-1] == "terminate_job" and process.returncode is None
    waiting = asyncio.create_task(process.wait())
    await asyncio.sleep(0)
    assert not waiting.done()
    case.state.code = 9
    assert await waiting == 9
    with pytest.raises(RuntimeError, match="尚未核实退出"):
        await process.aclose()
    assert "close_process" not in case.events and "close_job" not in case.events
    case.state.active = 0
    await process.aclose()
    assert case.events[-2:] == ["close_process", "close_job"]
    assert process.returncode == 9 and await process.wait() == 9


@pytest.mark.parametrize("executable,application", [
    ("server.exe", None),
    ("C:\\" + "long-directory\\" * 22 + "python.exe", "C:\\" + "long-directory\\" * 22 + "python.exe"),
])
async def test_absolute_executable_is_explicit_without_changing_path_search(native_process_case, executable, application):
    case = native_process_case
    process = case.open((executable, "argument with space"))
    assert case.state.application == application
    assert case.state.commandline.endswith('"argument with space"')
    case.state.code, case.state.active = 0, 0
    await process.aclose()


def test_windows_assignment_failure_never_resumes_and_waits_before_releasing_handles(native_process_case):
    case = native_process_case
    case.state.fail_assign = True
    with pytest.raises(OSError, match="原 Job 挂接失败"):
        case.open()
    assert "resume" not in case.events
    assert case.events.index("terminate_process") < case.events.index("wait") < case.events.index("close_process")
    assert case.events[-1] == "close_job"
    for descriptor in case.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_windows_job_creation_failure_opens_no_pipes(native_process_case):
    case = native_process_case
    case.state.fail_job = True
    with pytest.raises(OSError, match="Job 创建失败"):
        case.open()
    assert not case.descriptors and not case.events
