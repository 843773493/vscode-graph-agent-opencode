"""Gateway 对目标本机原生扩展管理服务的发现、接管和生命周期。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.backend_identity import load_or_create_backend_id
from app.core.env import get_project_root
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.core.trace_middleware import require_current_request_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.runtime.process_lifecycle import (
    AdoptedManagedProcess,
    ManagedProcess,
    _spawn_logged_process,
)
from app.gateway.runtime.process_logs import ProcessLogFile, ProcessLogStore
from app.schemas.runtime_logging import RuntimeLoggingStatus
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    InstalledProfile,
    NativeExtensionPublication,
    NativeFileRead,
    NativeFileStat,
    NativeManagementEnvironment,
    ProfileExtensionInventory,
    UninstalledProfile,
)
from app.services.infrastructure.extension_host.package_files import (
    publication_file,
)
from app.services.infrastructure.extension_host.runtime import (
    NodeHostRuntime,
    load_node_host_runtime,
)

logger = logging.getLogger(__name__)
Result = TypeVar("Result", bound=BaseModel)
_INSTANCE_FIELDS = {
    "backend_id", "process_id", "runtime_instance_id", "base_url", "commit",
    "vscode_version", "node_version",
}


class ExtensionManagementInstance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    backend_id: str = Field(min_length=1)
    process_id: int = Field(gt=0)
    runtime_instance_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    vscode_version: str = Field(min_length=1)
    node_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")

    def validate_target(self, *, backend_id: str, runtime: NodeHostRuntime) -> None:
        parsed = urlsplit(self.base_url)
        if (self.backend_id != backend_id or self.commit != runtime.commit
            or self.vscode_version != runtime.vscode_version or self.node_version != runtime.node_version
            or parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise RuntimeError("既存扩展管理实例与本 Gateway 安装域或运行时不匹配")


class GatewayExtensionManagement:
    """唯一 Gateway handle；它不拥有第二个安装锁，串行准入由 Node 服务提供。"""

    def __init__(self, *, backend_id: str, runtime: NodeHostRuntime, directory: Path,
                 environment: Mapping[str, str], log_dir: Path, logging_startup: Mapping[str, object],
                 startup_timeout: float = 10, operation_timeout: float = 120) -> None:
        self.backend_id, self.runtime, self.directory = backend_id, runtime, directory
        self._environment = dict(environment)
        self._log_store = ProcessLogStore(log_dir)
        self._logging_startup = dict(logging_startup)
        self._startup_timeout, self._operation_timeout = startup_timeout, operation_timeout
        self.process: ManagedProcess | AdoptedManagedProcess | None = None
        self.instance: ExtensionManagementInstance | None = None
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        self._detached = False
        self.preserve_on_close = False
        self._failure: BaseException | None = None
        self._download_admission: tuple[str, str] | None = None
        self._shutdown_deadline: float | None = None

    @classmethod
    def from_environment(cls, home: Path, *, logging_startup: Mapping[str, object]) -> GatewayExtensionManagement | None:
        runtime = load_node_host_runtime()
        if runtime is None:
            return None
        backend_id = load_or_create_backend_id(home)
        environment = os.environ.copy()
        environment["BOXTEAM_PROJECT_ROOT"] = str(get_project_root())
        return cls(backend_id=backend_id, runtime=runtime,
            directory=home / "state/backend/extension-management" / backend_id,
            environment=environment, log_dir=home / "state/logs", logging_startup=logging_startup)

    @property
    def drained(self) -> bool:
        return self.process is None and self._client is None

    def assert_healthy(self) -> None:
        if self._failure is not None:
            raise RuntimeError("Gateway 常驻扩展管理组件已失败；不自动重启或重放操作") from self._failure
        if self._closing or self._detached or self.process is None or not self.process.is_alive() or self.instance is None:
            raise RuntimeError("Gateway 常驻扩展管理组件未运行")

    async def _health(self, instance: ExtensionManagementInstance) -> RuntimeLoggingStatus:
        assert self._client is not None
        async with asyncio.timeout(self._startup_timeout):
            response = await self._client.get(f"{instance.base_url}/health",
                headers={"X-Local-Token": get_gateway_local_token()}, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ValueError("扩展管理服务健康响应无效")
        observed = ExtensionManagementInstance.model_validate({key: payload[key] for key in _INSTANCE_FIELDS})
        observed.validate_target(backend_id=self.backend_id, runtime=self.runtime)
        if observed != instance:
            raise RuntimeError("扩展管理服务健康身份与 service.json 不匹配")
        status = await self._runtime_logging_status(instance)
        if status.runtime_instance_id != instance.runtime_instance_id or status.service != "extension-management":
            raise RuntimeError("扩展管理服务日志状态与运行实例不匹配")
        return status

    async def _runtime_logging_status(self, instance: ExtensionManagementInstance) -> RuntimeLoggingStatus:
        assert self._client is not None
        response = await self._client.get(f"{instance.base_url}/internal/runtime-logging",
            headers={"X-Local-Token": get_gateway_local_token()}, follow_redirects=False)
        response.raise_for_status()
        envelope = response.json()
        if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
            raise ValueError("扩展管理日志证明缺少 data")  # noqa: TRY004 - 保持现有回执校验的 ValueError 契约。
        state = RuntimeLoggingStatus.model_validate(envelope["data"])
        if state.service != "extension-management" or state.pid != instance.process_id:
            raise RuntimeError("扩展管理日志证明与 service.json 进程身份不匹配")
        return state

    async def _existing_instance(self) -> ExtensionManagementInstance | None:
        self.directory.mkdir(parents=True, exist_ok=True)
        record = self.directory / "service.json"
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        instance = ExtensionManagementInstance.model_validate(payload)
        instance.validate_target(backend_id=self.backend_id, runtime=self.runtime)
        if not AdoptedManagedProcess(instance.process_id).is_alive():
            # 管理进程退出会丢失内存引用；其它 Backend 的 Node/Web 实例和文件读取仍可能存活。
            # TODO(4.4)：由全部原运行 owner 提供同代实例与读取的退出证明后，才能解除隔离并重建。
            raise RuntimeError("旧扩展实例与包读取尚无完整退出证明，拒绝重建或回收扩展管理安装域: "
                f"backend_id={instance.backend_id}, runtime_instance_id={instance.runtime_instance_id}, "
                f"process_id={instance.process_id}；须先由原运行 owner 停止并核实全部旧实例")
        self._client = httpx.AsyncClient(trust_env=False, timeout=self._operation_timeout)
        try:
            status = await self._health(instance)
        except (httpx.HTTPError, OSError) as error:
            await self._client.aclose()
            self._client = None
            # 有记录但无法证明原实例已经退出时，不能抢启第二个服务。
            raise RuntimeError("已有扩展管理实例仍存活但健康证明失败；拒绝并行启动") from error
        self.process = AdoptedManagedProcess(instance.process_id,
            self._log_store.adopt("extension-management", None, status.model_dump(mode="json")))
        self.instance = instance
        return instance

    async def start(self) -> None:
        if self._failure is not None:
            raise RuntimeError("扩展管理 owner 已失败，拒绝重试") from self._failure
        try:
            if await self._existing_instance() is not None:
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            env = dict(self._environment)
            env["BOXTEAM_RUNTIME_CONTROL_TOKEN"] = get_gateway_local_token()
            env["BOXTEAM_RUNTIME_LOGGING"] = json.dumps(self._logging_startup, separators=(",", ":"))
            env["PYTHONUNBUFFERED"] = "1"
            entrypoint = self.runtime.entrypoint.with_name("extensionManagement.mjs")
            command = [str(self.runtime.executable), str(entrypoint), str(self.directory), self.backend_id]
            log_file = self._log_store.open("extension-management")
            # 安装域已由绝对参数指定；不把可任意嵌套的数据目录作为 Windows 进程工作目录。
            process = _spawn_logged_process(command, log_file=log_file, cwd=entrypoint.parent, env=env, detached=True)
            self.process = ManagedProcess(process=process, log_file=log_file)
            await self._wait_for_instance(log_file)
        except BaseException as error:
            self._failure = error
            try:
                if isinstance(self.process, AdoptedManagedProcess):
                    await run_cleanup_shielded(lambda: self.close(preserve=True))
                else:
                    await run_cleanup_shielded(self.close)
            except BaseException as close_error:  # noqa: BLE001 - 取消与清理失败必须连同原异常保留。
                raise BaseExceptionGroup("扩展管理启动失败且清理失败", [error, close_error]) from None
            raise

    async def _wait_for_instance(self, log_file: ProcessLogFile) -> None:
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        record = self.directory / "service.json"
        self._client = httpx.AsyncClient(trust_env=False, timeout=self._operation_timeout)
        while asyncio.get_running_loop().time() < deadline:
            if self.process is None:
                raise RuntimeError("扩展管理 Node owner 在启动期间丢失")
            if not self.process.is_alive():
                raise RuntimeError("扩展管理 Node 进程在发布 service.json 前退出")
            try:
                payload = json.loads(record.read_text(encoding="utf-8"))
            except FileNotFoundError:
                await asyncio.sleep(0.05)
                continue
            instance = ExtensionManagementInstance.model_validate(payload)
            instance.validate_target(backend_id=self.backend_id, runtime=self.runtime)
            if instance.process_id != self.process.pid:
                await asyncio.sleep(0.05)
                continue
            try:
                async with asyncio.timeout(max(0, deadline - asyncio.get_running_loop().time())):
                    status = await self._health(instance)
            except (httpx.ConnectError, httpx.TimeoutException):
                await asyncio.sleep(0.05)
                continue
            expected = log_file.sink
            actual = None if status.log_sink is None else status.log_sink.model_dump()
            if actual != expected:
                raise RuntimeError("新扩展管理实例日志描述符与原 ProcessLogStore 不匹配")
            assert isinstance(self.process, ManagedProcess)
            self.process.confirm_service_identity(instance.process_id)
            self.instance = instance
            return
        raise TimeoutError("扩展管理 Node 服务未在期限内发布 service.json")

    async def _request(self, command: Mapping[str, JsonValue], *, response_type: type[Result],
                       download_base_url: str | None = None) -> Result:
        return await self._settle(self._request_locked(command, response_type=response_type,
            download_base_url=download_base_url))

    async def _request_locked(self, command: Mapping[str, JsonValue], *, response_type: type[Result],
                              download_base_url: str | None = None) -> Result:
        async with self._lock:
            self.assert_healthy()
            assert self._client is not None and self.instance is not None
            native_command = dict(command)
            if command.get("method") in {"install", "install-gallery"} and download_base_url is not None:
                request_id = require_current_request_id("扩展下载传输")
                capability = uuid4().hex
                self._download_admission = capability, request_id
                native_command["request_transport"] = {
                    "url": f"{download_base_url}/api/gateway/internal/extension-management/{self.backend_id}/downloads/{capability}",
                    "request_id": request_id,
                }
            try:
                response = await self._client.post(f"{self.instance.base_url}/management",
                    headers={"X-Local-Token": get_gateway_local_token()}, json=native_command, follow_redirects=False)
                response.raise_for_status()
                reply = response.json()
                if not isinstance(reply, dict):
                    raise ValueError("扩展管理控制回执无效")  # noqa: TRY004 - 保持现有控制回执的 ValueError 契约。
                if set(reply) == {"error", "code"}:
                    if not isinstance(reply["error"], str) or not isinstance(reply["code"], str):
                        raise ValueError("扩展管理错误回执字段无效")
                    raise ExtensionManagementError(reply["error"], reply["code"])
                if set(reply) != {"result"}:
                    raise ValueError("扩展管理控制回执不完整")
                result = response_type.model_validate(reply["result"])
                if isinstance(result, NativeManagementEnvironment):
                    service = result.reference_service
                    if (service.backend_id != self.backend_id or service.runtime_instance_id != self.instance.runtime_instance_id
                        or service.url != f"{self.instance.base_url}/references"):
                        raise ValueError("运行引用服务与已接管的原安装实例不匹配")
                return result
            except ExtensionManagementError:
                raise
            except httpx.HTTPStatusError as error:
                try:
                    reply = error.response.json()
                except ValueError as parse_error:
                    self._failure = parse_error
                    raise
                if (isinstance(reply, dict) and set(reply) == {"error", "code"}
                    and isinstance(reply["error"], str) and isinstance(reply["code"], str)):
                    raise ExtensionManagementError(reply["error"], reply["code"]) from error
                self._failure = error
                raise
            except BaseException as error:
                self._failure = error
                raise
            finally:
                # 未知回执后，已受理的 Node 工作仍可结清下载；正常终态立即撤销本次能力。
                if self._failure is None:
                    self._download_admission = None

    def authorize_download(self, capability: str, request_id: str) -> None:
        if self._download_admission != (capability, request_id):
            raise PermissionError("扩展下载不属于本 Gateway 尚未结清的原安装请求")

    @staticmethod
    async def _settle(awaitable: Awaitable[Result]) -> Result:
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            async def wait_for_task() -> None:
                await task
            await run_cleanup_shielded(wait_for_task)
            raise

    async def request(self, command: Mapping[str, JsonValue], *,
                      download_base_url: str | None = None) -> dict[str, JsonValue]:
        operation = command.get("method")
        if operation in {"extension-file-stat", "extension-file-read"}:
            try:
                result = await self._read_publication(command,
                    operation="stat" if operation == "extension-file-stat" else "read")
            except ExtensionManagementError as error:
                return {"error": str(error), "code": error.code}
            return {"result": result.model_dump(mode="json")}
        if operation not in {"inventory", "environment", "install", "install-gallery", "metadata", "uninstall"}:
            raise ValueError("未登记的扩展安装管理操作")
        response_type = {
            "inventory": ProfileExtensionInventory,
            "environment": NativeManagementEnvironment,
            "install": InstalledProfile,
            "install-gallery": InstalledProfile,
            "metadata": ProfileExtensionInventory,
            "uninstall": UninstalledProfile,
        }[operation]
        try:
            result = await self._request(command, response_type=response_type, download_base_url=download_base_url)
        except ExtensionManagementError as error:
            return {"error": str(error), "code": error.code}
        return {"result": result.model_dump(mode="json")}

    async def _read_publication(self, command: Mapping[str, JsonValue], *, operation: Literal["stat", "read"]) -> NativeFileStat | NativeFileRead:
        return await self._settle(self._read_publication_locked(command, operation=operation))

    async def _read_publication_locked(self, command: Mapping[str, JsonValue], *, operation: Literal["stat", "read"]) -> NativeFileStat | NativeFileRead:
        async with self._lock:
            self.assert_healthy()
            assert self._client is not None and self.instance is not None
            extension_key = command.get("extension_key")
            profile_id = command.get("profile_id")
            extension_id = command.get("extension_id")
            current_member = command.get("current_profile_member")
            segments = command.get("relative_segments")
            if (not isinstance(extension_key, str) or not isinstance(profile_id, str)
                or not isinstance(extension_id, str) or not isinstance(current_member, bool)
                or not isinstance(segments, list) or any(not isinstance(item, str) for item in segments)):
                raise TypeError("扩展包发布请求缺少精确定位字段")
            node_request = {"method": "extension-publication", "profile_id": profile_id,
                "extension_id": extension_id, "extension_key": extension_key,
                "current_profile_member": current_member}
            async with self._client.stream("POST", f"{self.instance.base_url}/publication",
                headers={"X-Local-Token": get_gateway_local_token()}, json=node_request, follow_redirects=False) as response:
                try:
                    response.raise_for_status()
                    lines = response.aiter_lines()
                    reply = json.loads(await anext(lines))
                    if not isinstance(reply, dict):
                        raise ValueError("扩展包发布描述回执无效")  # noqa: TRY004 - 保持现有发布回执的 ValueError 契约。
                    if set(reply) == {"error", "code"}:
                        if not isinstance(reply["error"], str) or not isinstance(reply["code"], str):
                            raise ValueError("扩展包发布错误回执字段无效")
                        raise ExtensionManagementError(reply["error"], reply["code"])
                    if set(reply) != {"result"}:
                        raise ValueError("扩展包发布描述回执不完整")
                    publication = NativeExtensionPublication.model_validate(reply["result"])
                except ExtensionManagementError:
                    raise
                except BaseException as error:
                    self._failure = error
                    raise
                return await run_sync_shielded(publication_file, publication, extension_key=extension_key,
                    relative_segments=segments, file_operation=operation)

    async def call(self, command: Mapping[str, JsonValue], *, response_type: type[Result],
                   download_base_url: str | None = None) -> Result:
        operation = command.get("method")
        if operation in {"extension-file-stat", "extension-file-read"}:
            result = await self._read_publication(command,
                operation="stat" if operation == "extension-file-stat" else "read")
            return response_type.model_validate(result.model_dump(mode="json"))
        reply = await self.request(command, download_base_url=download_base_url)
        if set(reply) == {"error", "code"}:
            raise ExtensionManagementError(str(reply["error"]), str(reply["code"]))
        return response_type.model_validate(reply["result"])

    async def _settle_downloads(self) -> None:
        if self._download_admission is None:
            return
        assert self._client is not None and self.instance is not None
        # 控制回执未知不等于安装结束。原 environment 与安装共用串行队列，
        # 等它返回后才能关闭本代下载入口；不重放原安装或另建 drain 接口。
        response = await self._client.post(f"{self.instance.base_url}/management",
            headers={"X-Local-Token": get_gateway_local_token()},
            json={"method": "environment"}, follow_redirects=False)
        response.raise_for_status()
        reply = response.json()
        if not isinstance(reply, dict) or set(reply) != {"result"}:
            raise ValueError("扩展管理交接没有确认原安装队列结清")
        NativeManagementEnvironment.model_validate(reply["result"])
        self._download_admission = None

    def _begin_shutdown(self) -> float:
        self._closing = True
        now = asyncio.get_running_loop().time()
        if self._shutdown_deadline is None:
            self._shutdown_deadline = now + self._operation_timeout
        if now >= self._shutdown_deadline:
            raise TimeoutError("扩展管理停机总期限已耗尽，原工作或实际进程尚未结清")
        return self._shutdown_deadline

    async def quiesce(self) -> None:
        """在 HTTP listener 关闭前停止新准入，保留回调直到原安装真正结清。"""
        async with asyncio.timeout_at(self._begin_shutdown()):
            async with self._lock:
                await self._settle_downloads()

    async def close(self, *, preserve: bool | None = None) -> None:
        preserve = self.preserve_on_close if preserve is None else preserve
        self._closing = True
        if self.process is None:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._download_admission = None
            return
        deadline = self._begin_shutdown()
        # 监听关闭前结清、HTTP shutdown 和实际进程退出共用总期限，不能逐阶段重开预算。
        async with asyncio.timeout_at(deadline), self._lock:
            if preserve:
                await self._settle_downloads()
                if self._client is not None:
                    await self._client.aclose()
                    self._client = None
                self.process.detach()
                self.process = None
                self.instance = None
                self._detached = True
                self._download_admission = None
                return
            if self.instance is None and isinstance(self.process, ManagedProcess):
                # 只处理本轮未通过身份证明的 child；原进程工具的软停／强停各占剩余预算一半。
                remaining = max(0, deadline - asyncio.get_running_loop().time())
                await run_sync_shielded(self.process.close, timeout_seconds=remaining / 2)
                self.process = None
                self.instance = None
                if self._client is not None:
                    await self._client.aclose()
                    self._client = None
                return
            if self._client is not None and self.instance is not None:
                response = await self._client.post(f"{self.instance.base_url}/shutdown",
                    headers={"X-Local-Token": get_gateway_local_token()}, follow_redirects=False)
                response.raise_for_status()
                if response.json() != {"drained": True}:
                    raise ValueError("扩展管理服务没有确认 drain")
            if self.process is not None:
                while self.process.is_alive():
                    await asyncio.sleep(0.02)
                await run_sync_shielded(self.process.close, timeout_seconds=0)
                self.process = None
                self.instance = None
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._detached = False
            self._download_admission = None


@asynccontextmanager
async def extension_management_lifespan(home: Path, *, logging_startup: Mapping[str, object],
                                        preserve_for_handoff: Callable[[], bool] | None = None
                                        ) -> AsyncIterator[GatewayExtensionManagement | None]:
    owner = GatewayExtensionManagement.from_environment(home, logging_startup=logging_startup)
    if owner is None:
        yield None
        return
    await owner.start()
    try:
        yield owner
    finally:
        preserve = preserve_for_handoff() if preserve_for_handoff is not None else False
        await run_cleanup_shielded(lambda: owner.close(preserve=preserve))
