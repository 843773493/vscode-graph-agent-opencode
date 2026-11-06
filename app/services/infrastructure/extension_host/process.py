"""直接管理原 Node 宿主及 IPC，不引入 Node broker 或第二套 RPC。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import BinaryIO

from app.core.child_process import OwnedChildProcess
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime


class NodeExtensionHost:
    """一个已准入 generation 的一次性进程；运行时和路径只能由目标 owner 提供。"""

    def __init__(self, instance_id: str, *, runtime: NodeHostRuntime,
                 directory: Path, environment: Mapping[str, str], output: BinaryIO,
                 startup_timeout: float) -> None:
        if not runtime.executable.is_absolute() or not runtime.entrypoint.is_absolute() or not directory.is_absolute():
            raise ValueError("Node 宿主运行时、入口和基础设施目录必须是目标本机绝对路径")
        if startup_timeout <= 0:
            raise ValueError("Node 宿主启动期限必须为正数")
        self.child = OwnedChildProcess(instance_id)
        self._runtime, self._directory = runtime, directory
        self._environment, self._output = dict(environment), output
        self._startup_timeout = startup_timeout
        self._started = False

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        if self._started:
            raise RuntimeError("Node 宿主 generation 不能重复启动或重连")
        self._started = True
        # TODO: macOS 验证短 IPC 路径后接入；不回退到公开 TCP。
        if sys.platform not in {"linux", "win32"}:
            raise NotImplementedError("Node 宿主私有 IPC 当前仅支持 Linux 和 Windows 目标")
        self._directory.mkdir(parents=True, exist_ok=True)
        cleanup = AsyncExitStack()
        try:
            loop = asyncio.get_running_loop()
            if sys.platform == "win32":
                from app.runtime.windows_ipc import WindowsPipeServer

                server = WindowsPipeServer()
                address, accepted = server.address, server.connection
            else:
                # IPC 是短暂运行态，正文及输出仍由调用方的 Backend 基础设施目录持有。
                temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="ipc-", dir=self._directory))
                address = str(Path(temporary) / "host.sock")
                if Path("/proc/self/fd").is_dir():
                    descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
                    cleanup.callback(os.close, descriptor)
                    # 正式测试和较长 BOXTEAM_HOME 也可使用原目录；不搬正文或创建第二存储根。
                    address = f"/proc/{os.getpid()}/fd/{descriptor}/host.sock"
                accepted = loop.create_future()

                def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                    if accepted.done():
                        writer.close()
                    else:
                        accepted.set_result((reader, writer))

                server = await asyncio.start_unix_server(accept, path=address)

            async def close_connection() -> None:
                server.close()
                try:
                    if accepted.done() and not accepted.cancelled():
                        _, writer = accepted.result()
                        writer.close()
                        await writer.wait_closed()
                    else:
                        accepted.cancel()
                finally:
                    await server.wait_closed()

            # 沿上游 writeExtHostConnection 的互斥语义，只提供一种实际 IPC 模式。
            environment = {key: value for key, value in self._environment.items() if key not in {
                "VSCODE_EXTHOST_IPC_HOOK", "VSCODE_EXTHOST_WILL_SEND_SOCKET", "VSCODE_WILL_SEND_MESSAGE_PORT",
            }}
            environment.update(VSCODE_EXTHOST_IPC_HOOK=address, VSCODE_RECONNECTION_GRACE_TIME="0")
            exited: asyncio.Task[int] | None = None

            async def settle_exit() -> None:
                try:
                    if exited is not None:
                        if not exited.done():
                            exited.cancel()
                        result = (await asyncio.gather(exited, return_exceptions=True))[0]
                        if isinstance(result, Exception):
                            raise result
                finally:
                    await run_sync_shielded(self._output.flush)

            cleanup.push_async_callback(settle_exit)
            # start 取消时仍可能已取得真实句柄，必须在启动前登记回收。
            cleanup.push_async_callback(self.child.close)

            # 先断开原 IPC，原宿主可沿 renderer-closed 正常退出；其后才等待/终止进程组。
            cleanup.push_async_callback(close_connection)
            # 原生标准流直接写目标日志，不增加collector或在退出时丢弃未读尾部。
            await self.child.start([str(self._runtime.executable), str(self._runtime.entrypoint), "--useHostProxy=false"],
                                   env=environment, cwd=self._directory, stderr=-2, stdout=self._output)
            assert self.child.process is not None
            exited = asyncio.create_task(self.child.process.wait(), name=f"extension-exit:{self.child.state.instance_id}")
            done, _ = await asyncio.wait((accepted, exited), timeout=self._startup_timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError(f"Node 宿主 IPC 启动超时: instance_id={self.child.state.instance_id}")
            if exited in done:
                raise RuntimeError(f"Node 宿主在 IPC 准备期间退出: instance_id={self.child.state.instance_id}, returncode={exited.result()}")
            server.close()
            yield accepted.result()
        finally:
            await run_cleanup_shielded(cleanup.aclose)
