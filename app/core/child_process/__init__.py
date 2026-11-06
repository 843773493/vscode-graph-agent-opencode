"""直接子进程的唯一句柄、创建身份和退出核验。"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Literal, TextIO

import anyio
import psutil
from anyio.abc import Process
from pydantic import ConfigDict

from app.core.shielded_cleanup import run_cleanup_shielded

_EXIT_WAIT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ChildProcessState:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    instance_id: str
    phase: Literal["reserved", "running", "exited", "not_started"] = "reserved"
    pid: int | None = None
    created_at: float | None = None
    returncode: int | None = None
    exit_verified: bool = False

    def __post_init__(self) -> None:
        if not self.instance_id or self.pid is not None and self.pid <= 0:
            raise ValueError("子进程实例身份或 PID 无效")
        if self.exit_verified != (self.phase in {"exited", "not_started"}):
            raise ValueError("子进程退出核验与阶段不一致")
        if self.phase == "exited" and (self.pid is None or self.returncode is None):
            raise ValueError("已退出子进程缺少原 PID 或 wait 退出码")
        if self.phase in {"reserved", "not_started"} and (self.pid is not None or self.returncode is not None):
            raise ValueError("尚未创建子进程不能带运行身份")


class OwnedChildProcess:
    """只监督自己创建的句柄；不把发出 kill 或退出上下文当作退出证明。"""

    def __init__(self, instance_id: str) -> None:
        self.state = ChildProcessState(instance_id=instance_id)
        self.process: Process | None = None
        self._group_empty: Callable[[], bool] | None = None
        self._terminate: Callable[[], None] | None = None
        self._kill: Callable[[], None] | None = None
        self._creating: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()

    async def start(self, command: Sequence[str], *, env: Mapping[str, str],
                    cwd: str | Path | None, stderr: TextIO | Literal[-2] = sys.stderr) -> None:
        if self._creating is not None:
            raise RuntimeError("子进程身份不能重复启动")

        async def create() -> None:
            if os.name == "nt":
                from app.runtime.windows_process import open_windows_process

                process = await asyncio.to_thread(open_windows_process, command, env=env, cwd=cwd, stderr=stderr)
                self.process = process
                self._group_empty = process.group_empty
                self._terminate, self._kill = process.terminate, process.kill
            else:
                self.process = await anyio.open_process(command, env=env, cwd=cwd, stderr=stderr, start_new_session=True)
                group_id = self.process.pid

                def group_empty() -> bool:
                    try:
                        os.killpg(group_id, 0)
                    except ProcessLookupError:
                        return True
                    return False

                def send(signum: int) -> None:
                    try:
                        os.killpg(group_id, signum)
                    except ProcessLookupError:
                        # 原进程组已不存在；最终 wait 仍须核实原句柄。
                        return

                self._group_empty = group_empty
                self._terminate = lambda: send(signal.SIGTERM)
                self._kill = lambda: send(signal.SIGKILL)
            process = self.process
            assert process is not None
            self.state = replace(self.state, phase="running", pid=process.pid)
            try:
                created_at = await asyncio.to_thread(lambda: psutil.Process(process.pid).create_time())
            except psutil.NoSuchProcess:
                # 快速退出仍由原 Process.wait 核实，不能填造创建时间。
                created_at = None
            self.state = replace(self.state, created_at=created_at)

        self._creating = asyncio.create_task(create())
        try:
            await asyncio.shield(self._creating)
        except asyncio.CancelledError:
            async def finish_create() -> None:
                assert self._creating is not None
                await self._creating

            await run_cleanup_shielded(finish_create)
            raise

    async def _wait(self) -> bool:
        assert self.process is not None and self._group_empty is not None
        deadline = asyncio.get_running_loop().time() + _EXIT_WAIT_SECONDS
        try:
            async with asyncio.timeout(_EXIT_WAIT_SECONDS):
                await self.process.wait()
        except TimeoutError:
            return False
        while not self._group_empty():
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    async def close(self) -> None:
        async with self._close_lock:
            if self.state.exit_verified:
                return
            if self._creating is not None:
                # 创建失败的原异常已交给 start；已有句柄仍须回收。
                await asyncio.gather(self._creating, return_exceptions=True)
            process = self.process
            if process is None:
                self.state = replace(self.state, phase="not_started", exit_verified=True)
                return
            if process.stdin is not None:
                await process.stdin.aclose()
            if not await self._wait():
                assert self._terminate is not None and self._kill is not None
                self._terminate()
                if not await self._wait():
                    self._kill()
                    if not await self._wait():
                        self.state = replace(self.state, returncode=process.returncode)
                        raise TimeoutError(f"子进程或其原进程组退出未核实: instance_id={self.state.instance_id}, pid={process.pid}")
            # 两个事实都来自原句柄／容器，不能按 Backend 或任务退出推断。
            returncode = await process.wait()
            await process.aclose()
            self.state = replace(self.state, phase="exited", returncode=returncode, exit_verified=True)


__all__ = ["ChildProcessState", "OwnedChildProcess"]
