"""Gateway runtime 测试共用的轻量进程替身。"""

import subprocess
from collections.abc import Callable

import pytest


class ManagedProcessStub:
    pid = 4321

    def __init__(self, *, timeout_once: bool) -> None:
        self.returncode: int | None = None
        self._timeout_once = timeout_once
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self._timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("test", timeout)
        self.returncode = 0
        return 0


@pytest.fixture
def managed_process_stub_factory() -> Callable[..., ManagedProcessStub]:
    return ManagedProcessStub
