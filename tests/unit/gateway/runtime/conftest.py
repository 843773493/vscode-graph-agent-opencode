"""Gateway runtime 测试共用的轻量进程替身。"""

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import psutil
import pytest


class ManagedProcessStub:
    pid = 4321

    def __init__(self, *, timeout_once: bool) -> None:
        self.returncode: int | None = None
        self._timeout_once = timeout_once
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self._timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("test", timeout)
        self.returncode = 0
        return 0


@pytest.fixture
def managed_process_stub_factory() -> Callable[..., ManagedProcessStub]:
    return ManagedProcessStub


@dataclass(slots=True)
class WindowsProcessStub:
    pid: int
    parent_pid: int | None
    starttime: float
    exit_on_terminate: bool = True
    alive: bool = True
    terminate_calls: int = 0
    kill_calls: int = 0

    @property
    def info(self) -> dict[str, int | float | None]:
        return {"pid": self.pid, "create_time": self.starttime}

    def create_time(self) -> float:
        if not self.alive:
            raise psutil.NoSuchProcess(self.pid)
        return self.starttime

    def status(self) -> str:
        if not self.alive:
            raise psutil.NoSuchProcess(self.pid)
        return psutil.STATUS_RUNNING

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.exit_on_terminate:
            self.alive = False

    def kill(self) -> None:
        self.kill_calls += 1
        self.alive = False


class WindowsProcessTreeStub:
    def __init__(self) -> None:
        self.parent_map_reads = 0
        self.process_iter_attrs: list[list[str]] = []
        self.processes: dict[int, WindowsProcessStub] = {
            os.getpid(): WindowsProcessStub(os.getpid(), None, psutil.Process().create_time()),
        }

    def add_process(
        self,
        pid: int,
        *,
        parent_pid: int | None,
        starttime: float,
        exit_on_terminate: bool = True,
    ) -> WindowsProcessStub:
        process = WindowsProcessStub(
            pid=pid,
            parent_pid=parent_pid,
            starttime=starttime,
            exit_on_terminate=exit_on_terminate,
        )
        self.processes[pid] = process
        return process

    def process_iter(self, *, attrs: list[str]) -> list[WindowsProcessStub]:
        assert "ppid" not in attrs, "父子关系应批量读取，不能逐进程枚举整机"
        self.process_iter_attrs.append(attrs)
        return [process for process in self.processes.values() if process.alive]

    def parent_pid_map(self) -> dict[int, int]:
        self.parent_map_reads += 1
        return {
            process.pid: process.parent_pid
            for process in self.processes.values()
            if process.alive and process.parent_pid is not None
        }

    def get_process(self, pid: int | None = None) -> WindowsProcessStub:
        pid = os.getpid() if pid is None else pid
        process = self.processes.get(pid)
        if process is None or not process.alive:
            raise psutil.NoSuchProcess(pid)
        return process


@pytest.fixture
def windows_process_tree_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> WindowsProcessTreeStub:
    tree = WindowsProcessTreeStub()
    monkeypatch.setattr(psutil, "Process", tree.get_process)
    monkeypatch.setattr(psutil, "process_iter", tree.process_iter)
    monkeypatch.setattr(psutil, "_ppid_map", tree.parent_pid_map)
    return tree
