"""Backend 直管原生扩展管理组件；安装事实只由 Code OSS profile 服务保存。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, TextIO, TypeVar
from uuid import uuid4

import portalocker
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel, ConfigDict, JsonValue

from app.core.child_process import OwnedChildProcess
from app.core.shielded_cleanup import run_cleanup_shielded
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationWriteOptions,
)
from app.schemas.gateway.extension_host import NativeFileURI
from app.schemas.gateway.extension_installation import (
    ExtensionUninstallResult,
    ProfileExtensionInventory,
    VsixInstallResult,
)
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime


class InstalledProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    installed: VsixInstallResult
    inventory: ProfileExtensionInventory


class UninstalledProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    uninstalled: ExtensionUninstallResult
    inventory: ProfileExtensionInventory


class ExtensionManagementError(RuntimeError):
    """原安装／扫描服务已经明确回报失败，传输本身仍可继续查询。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class NativeManagementEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    app_root_uri: NativeFileURI
    global_storage_home_uri: NativeFileURI
    workspace_storage_home_uri: NativeFileURI
    profile_id: str
    settings_native_uri: NativeFileURI


class NativeConfigurationLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    native_uri: NativeFileURI


class NativeFileStat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: int
    ctime: float
    mtime: float
    size: int
    permissions: int | None


class NativeFileRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    content_base64: str


class NativeConfigurationFolder(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    native_uri: NativeFileURI
    name: str


class NativeConfigurationView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: str
    native_uri: NativeFileURI
    folders: tuple[NativeConfigurationFolder, ...]


Result = TypeVar("Result", bound=BaseModel)


class NodeExtensionManagement:
    """每次请求独占稳定安装域并排空原服务；不维护另一份安装清单。"""

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

    async def _start(self) -> dict[str, JsonValue] | None:
        entrypoint = self._runtime.entrypoint.with_name("extensionManagement.mjs")
        if not entrypoint.is_file():
            raise FileNotFoundError(f"目标专用运行时缺少扩展管理组件: {entrypoint}")
        self._directory.mkdir(parents=True, exist_ok=True)
        lease = portalocker.Lock(self._directory / "management.lock", mode="a", timeout=0)
        while True:
            if self._closing:
                raise RuntimeError("扩展管理组件已关闭，不能启动新请求")
            try:
                lease.acquire()
                break
            except portalocker.exceptions.AlreadyLocked:
                # 非阻塞获取只重试真实争用；权限等错误保留原异常。
                await asyncio.sleep(0.02)
        self._lease = lease
        if self._closing:
            raise RuntimeError("扩展管理组件已关闭，不能启动新请求")
        self.child = OwnedChildProcess(f"extension-management:{uuid4()}")
        logs = self._directory / "logs"
        logs.mkdir(exist_ok=True)
        self._output = (logs / f"{uuid4()}.log").open("x", encoding="utf-8")
        await self.child.start([str(self._runtime.executable), str(entrypoint), str(self._directory)],
                               env=self._environment, cwd=self._directory, stderr=self._output)
        assert self.child.process is not None and self.child.process.stdout is not None
        self._reader = BufferedByteReceiveStream(self.child.process.stdout)
        async with asyncio.timeout(self._startup_timeout):
            reply = await self._receive()
            if reply == {"ready": True}:
                return None
            if set(reply) == {"error", "code"}:
                return reply
            raise ValueError("扩展管理组件启动握手无效")

    async def _request(self, command: Mapping[str, JsonValue], *, response_type: type[Result],
                       on_snapshot: Callable[[Result], None] | None = None) -> Result:
        async with asyncio.timeout(self._operation_timeout), self._lock:
            if self._closing or self._failed:
                raise RuntimeError("扩展管理组件已关闭或控制结果未知，不能自动重启并重放")
            result: Result | None = None
            try:
                reply = await self._start()
                assert self.child.process is not None and self.child.process.stdin is not None
                if reply is None:
                    await self.child.process.stdin.send((json.dumps(command) + "\n").encode())
                    reply = await self._receive()
                if set(reply) not in ({"result"}, {"error", "code"}):
                    raise ValueError("扩展管理控制回执不完整")
                if "result" in reply:
                    result = response_type.model_validate(reply["result"])
                elif not isinstance(reply["error"], str) or not isinstance(reply["code"], str):
                    raise TypeError("扩展管理错误回执不是字符串")
                if await self._receive() != {"drained": True}:
                    raise ValueError("扩展管理组件未确认原异步任务已排空")
                returncode = await self.child.process.wait()
                if returncode != 0:
                    raise RuntimeError(f"扩展管理组件未正常退出: {returncode}")
            except BaseException as error:
                # 取消／断流／损坏回执后必须回收原进程；安装是否已发布只能在新 Backend 中读取原 profile，
                # 不能因为没有回执就自动重放写入。
                self._failed = True
                try:
                    await run_cleanup_shielded(self._close_process)
                except BaseException as close_error:  # noqa: BLE001 - 取消及回收失败必须同时保留。
                    raise BaseExceptionGroup("扩展管理操作及原进程回收均失败", [error, close_error]) from None
                raise
            try:
                if result is not None and on_snapshot is not None:
                    # 原进程已正常结算，安装锁尚未释放；同步冻结原扫描快照，避免准入跨过发布。
                    on_snapshot(result)
            finally:
                try:
                    await run_cleanup_shielded(self._close_process)
                except BaseException:
                    # 回收失败保留原 child／日志／锁，由原 close 重试，不重复清理或启动新进程。
                    self._failed = True
                    raise
            if "result" in reply:
                assert result is not None
                return result
            assert isinstance(reply["error"], str) and isinstance(reply["code"], str)
            raise ExtensionManagementError(reply["error"], reply["code"])

    async def inventory(self, *, on_snapshot: Callable[[ProfileExtensionInventory], None] | None = None) -> ProfileExtensionInventory:
        return await self._request({"method": "inventory"}, response_type=ProfileExtensionInventory, on_snapshot=on_snapshot)

    async def environment(self) -> NativeManagementEnvironment:
        return await self._request({"method": "environment"}, response_type=NativeManagementEnvironment)

    async def extension_file_stat(self, *, profile_id: str, extension_key: str, extension_id: str,
                                  relative_segments: list[str], current_profile_member: bool) -> NativeFileStat:
        return await self._request({"method": "extension-file-stat", "profile_id": profile_id,
                                   "extension_key": extension_key, "extension_id": extension_id, "relative_segments": list(relative_segments),
                                   "current_profile_member": current_profile_member},
                                   response_type=NativeFileStat)

    async def extension_file_read(self, *, profile_id: str, extension_key: str, extension_id: str,
                                  relative_segments: list[str], current_profile_member: bool) -> NativeFileRead:
        return await self._request({"method": "extension-file-read", "profile_id": profile_id,
                                   "extension_key": extension_key, "extension_id": extension_id, "relative_segments": list(relative_segments),
                                   "current_profile_member": current_profile_member},
                                   response_type=NativeFileRead)

    async def prepare_configuration_view(self, workspace_id: str, folders: list[dict[str, str]]) -> NativeConfigurationLocation:
        folder_values: list[JsonValue] = [{key: value for key, value in folder.items()} for folder in folders]
        return await self._request({"method": "configuration-view", "id": workspace_id, "folders": folder_values},
                                   response_type=NativeConfigurationLocation)

    async def configuration_stat(self, native_uri: str, *, view: NativeConfigurationView | None) -> NativeFileStat:
        return await self._request({"method": "configuration-stat", "native_uri": native_uri,
                                   "view": view.model_dump(mode="json") if view else None},
                                   response_type=NativeFileStat)

    async def configuration_read(self, native_uri: str, *, view: NativeConfigurationView | None) -> NativeFileRead:
        return await self._request({"method": "configuration-read", "native_uri": native_uri,
                                   "view": view.model_dump(mode="json") if view else None},
                                   response_type=NativeFileRead)

    async def configuration_write(self, native_uri: str, content_base64: str,
                                  options: ExtensionConfigurationWriteOptions, *,
                                  view: NativeConfigurationView | None) -> NativeFileStat:
        return await self._request({"method": "configuration-write", "native_uri": native_uri,
                                   "view": view.model_dump(mode="json") if view else None,
                                   "content_base64": content_base64, "options": options.model_dump(mode="json")},
                                   response_type=NativeFileStat)

    async def install(self, vsix_path: Path) -> InstalledProfile:
        if not vsix_path.is_absolute():
            raise ValueError("VSIX 必须由目标文件 owner 解析为本机绝对路径")
        return await self._request({"method": "install", "vsix_path": str(vsix_path)}, response_type=InstalledProfile)

    async def uninstall(self, *, profile_id: str, extension_id: str, version: str) -> UninstalledProfile:
        return await self._request({"method": "uninstall", "profile_id": profile_id,
                                   "extension_id": extension_id, "version": version}, response_type=UninstalledProfile)

    async def _close_process(self) -> None:
        await self.child.close()
        self._reader = None
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
