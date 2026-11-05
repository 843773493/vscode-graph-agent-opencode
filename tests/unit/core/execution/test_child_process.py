"""用可控内核等待证明 kill、创建取消和未核实退出的区别。"""

from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

from app.core import child_process
from app.core.child_process import OwnedChildProcess

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX 进程组信号边界；Windows Job Object 单独验证")


@pytest.fixture
def process_case(monkeypatch):
    exited, killed, creating, allow_create = (asyncio.Event() for _ in range(4))
    signals = []

    class Input:
        async def aclose(self):
            if normal:
                exited.set()

    class Process:
        pid = 123456789
        stdin = Input()
        returncode = None
        closed = False
        wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            await exited.wait()
            self.returncode = -signal.SIGKILL if signals else 0
            return self.returncode

        async def aclose(self):
            assert exited.is_set()
            self.closed = True

    process = Process()
    normal = True
    allow_create.set()

    async def open_process(*args, **kwargs):
        assert kwargs["start_new_session"]
        creating.set()
        await allow_create.wait()
        return process

    def killpg(pid, signum):
        assert pid == process.pid
        if signum == 0:
            if exited.is_set():
                raise ProcessLookupError
        else:
            signals.append(signum)
            if signum == signal.SIGKILL:
                killed.set()

    def noncooperative():
        nonlocal normal
        normal = False

    monkeypatch.setattr(child_process.anyio, "open_process", open_process)
    monkeypatch.setattr(child_process.os, "killpg", killpg)
    monkeypatch.setattr(child_process.psutil, "Process", lambda pid: SimpleNamespace(create_time=lambda: 123.0))
    monkeypatch.setattr(child_process, "_EXIT_WAIT_SECONDS", 0.03)
    return SimpleNamespace(process=process, exited=exited, killed=killed, creating=creating,
        allow_create=allow_create, signals=signals, noncooperative=noncooperative)


async def test_direct_handle_normal_exit_is_waited_and_verified(process_case):
    case = process_case
    owned = OwnedChildProcess("connection:normal")
    assert owned.state.phase == "reserved" and not owned.state.exit_verified
    await owned.start(["fixture"], env={}, cwd=None)
    assert owned.process is case.process and owned.state.pid == case.process.pid
    assert owned.state.created_at == 123.0
    await owned.close()
    assert owned.state.exit_verified and owned.state.returncode == 0
    assert case.process.wait_calls >= 2 and case.process.closed and not case.signals


async def test_kill_signal_does_not_verify_exit_before_original_wait(process_case):
    case = process_case
    case.noncooperative()
    owned = OwnedChildProcess("connection:forced")
    await owned.start(["fixture"], env={}, cwd=None)
    closing = asyncio.create_task(owned.close())
    await asyncio.wait_for(case.killed.wait(), 1)
    assert case.signals == [signal.SIGTERM, signal.SIGKILL]
    assert not closing.done() and not owned.state.exit_verified
    case.exited.set()
    await closing
    assert owned.state.returncode == -signal.SIGKILL and owned.state.exit_verified


async def test_failed_kernel_wait_keeps_unverified_handle(process_case):
    case = process_case
    case.noncooperative()
    owned = OwnedChildProcess("connection:unknown")
    await owned.start(["fixture"], env={}, cwd=None)
    with pytest.raises(TimeoutError, match="退出未核实"):
        await owned.close()
    assert not owned.state.exit_verified and not case.process.closed
    assert owned.process is case.process
    case.exited.set()
    await owned.close()


async def test_cancelled_spawn_waits_for_original_handle_before_propagating(process_case):
    case = process_case
    case.allow_create.clear()
    owned = OwnedChildProcess("connection:cancel")
    starting = asyncio.create_task(owned.start(["fixture"], env={}, cwd=None))
    await case.creating.wait()
    starting.cancel()
    await asyncio.sleep(0)
    assert not starting.done() and owned.process is None
    case.allow_create.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert owned.process is case.process and owned.state.pid == case.process.pid
    await owned.close()
    assert owned.state.exit_verified
