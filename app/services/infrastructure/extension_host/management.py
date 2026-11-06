"""Backend 直管原生扩展管理组件；安装事实只由 Code OSS profile 服务保存。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import IO, TextIO
from uuid import uuid4

import portalocker
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.child_process import OwnedChildProcess
from app.core.shielded_cleanup import run_cleanup_shielded
from app.schemas.gateway.extension_installation import ProfileExtensionInventory
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime


class _ControlValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class VsixInstallResult(_ControlValue):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)


class ExtensionManagementError(RuntimeError):
    """原安装／扫描服务已经明确回报失败，传输本身仍可继续查询。"""


class NodeExtensionManagement:
    """一个 Backend 的惰性管理进程；串行消费原服务，不维护另一份安装清单。"""

    def __init__(self, *, runtime: NodeHostRuntime, directory: Path,
                 environment: Mapping[str, str], startup_timeout: float,
                 operation_timeout: float) -> None:
        self._runtime, self._directory = runtime, directory
        self._environment = dict(environment)
        self._startup_timeout, self._operation_timeout = startup_timeout, operation_timeout
        self.child = OwnedChildProcess(f"extension-management:{uuid4()}")
        self._lock = asyncio.Lock()
        self._reader: BufferedByteReceiveStream | None = None
        self._output: TextIO | None = None
        self._lease: portalocker.Lock[IO[str]] | None = None
        self._closing = False
        self._failed = False

    def close_admission(self) -> None:
        self._closing = True

    @property
    def drained(self) -> bool:
        return self.child.state.exit_verified and self._output is None and self._lease is None

    async def _receive(self) -> dict[str, JsonValue]:
        assert self._reader is not None
        # 标准流控制消息有明确上限；拒绝损坏输出，不把剩余字节当作下一次回执。
        data = await self._reader.receive_until(b"\n", 16 * 1024 * 1024)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise TypeError("扩展管理组件没有返回控制对象")
        return value

    async def _start(self) -> None:
        entrypoint = self._runtime.entrypoint.with_name("extensionManagement.mjs")
        if not entrypoint.is_file():
            raise FileNotFoundError(f"目标专用运行时缺少扩展管理组件: {entrypoint}")
        self._directory.mkdir(parents=True, exist_ok=True)
        lease = portalocker.Lock(self._directory / "management.lock", mode="a", timeout=0)
        lease.acquire()
        self._lease = lease
        logs = self._directory / "logs"
        logs.mkdir(exist_ok=True)
        self._output = (logs / f"{uuid4()}.log").open("x", encoding="utf-8")
        await self.child.start([str(self._runtime.executable), str(entrypoint), str(self._directory)],
                               env=self._environment, cwd=self._directory, stderr=self._output)
        assert self.child.process is not None and self.child.process.stdout is not None
        self._reader = BufferedByteReceiveStream(self.child.process.stdout)
        async with asyncio.timeout(self._startup_timeout):
            if await self._receive() != {"ready": True}:
                raise ValueError("扩展管理组件启动握手无效")

    async def _request(self, command: dict[str, str]) -> JsonValue:
        async with self._lock:
            if self._closing or self._failed:
                raise RuntimeError("扩展管理组件已关闭或控制结果未知，不能自动重启并重放")
            try:
                if self._reader is None:
                    await self._start()
                assert self.child.process is not None and self.child.process.stdin is not None
                async with asyncio.timeout(self._operation_timeout):
                    await self.child.process.stdin.send((json.dumps(command) + "\n").encode())
                    reply = await self._receive()
                if set(reply) not in ({"result"}, {"error"}):
                    raise ValueError("扩展管理控制回执不完整")
            except BaseException as error:
                # 取消／断流后必须回收原进程；安装是否已发布只能在新 Backend 中读取原 profile，
                # 不能因为没有回执就自动重放写入。
                self._failed = True
                try:
                    await run_cleanup_shielded(self._close_process)
                except BaseException as close_error:  # noqa: BLE001 - 取消及回收失败必须同时保留。
                    raise BaseExceptionGroup("扩展管理操作及原进程回收均失败", [error, close_error]) from None
                raise
            if "error" in reply:
                raise ExtensionManagementError(str(reply["error"]))
            return reply["result"]

    async def inventory(self) -> ProfileExtensionInventory:
        return ProfileExtensionInventory.model_validate(await self._request({"method": "inventory"}))

    async def install(self, vsix_path: Path) -> VsixInstallResult:
        if not vsix_path.is_absolute():
            raise ValueError("VSIX 必须由目标文件 owner 解析为本机绝对路径")
        return VsixInstallResult.model_validate(await self._request({"method": "install", "vsix_path": str(vsix_path)}))

    async def _close_process(self) -> None:
        await self.child.close()
        if self._output is not None:
            self._output.close()
            self._output = None
        if self._lease is not None:
            self._lease.release()
            self._lease = None

    async def close(self) -> None:
        self.close_admission()
        async with self._lock:
            await run_cleanup_shielded(self._close_process)
