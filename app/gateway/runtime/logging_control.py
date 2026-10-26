"""通过既有本机管理通道读取、应用并核实真实进程的日志状态。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from app.core.logging_config import (
    RuntimeLoggingConflictError,
    apply_runtime_logging,
    runtime_logging_state,
)
from app.schemas.runtime_logging import (
    RuntimeLoggingApply,
    RuntimeLoggingHttpService,
    RuntimeLoggingService,
    RuntimeLoggingStatus,
)

if TYPE_CHECKING:
    from app.gateway.registry import GatewayWorkspaceRegistry
    from app.gateway.runtime.process_lifecycle import ManagedProcessHandle

_CONTROL_PATHS: dict[RuntimeLoggingHttpService, str] = {
    "gateway": "/api/gateway/internal/runtime-logging",
    "backend": "/api/v1/internal/runtime-logging",
    "terminal": "/internal/runtime-logging",
    "browser": "/internal/runtime-logging",
}


@dataclass(slots=True)
class RuntimeLoggingProcess:
    service: RuntimeLoggingService
    workspace_ids: tuple[str, ...]
    status: Callable[[], Awaitable[RuntimeLoggingStatus]]
    apply: Callable[[RuntimeLoggingApply], Awaitable[RuntimeLoggingStatus]]
    alive: Callable[[], bool]

    async def read(self) -> RuntimeLoggingStatus:
        if not self.alive():
            raise RuntimeError("日志目标进程已退出")
        state = await self.status()
        if not self.alive():
            raise RuntimeError("日志目标进程在确认期间退出")
        return state


def runtime_logging_processes(
    registry: GatewayWorkspaceRegistry, control: RuntimeLoggingProcessControl,
) -> list[RuntimeLoggingProcess]:
    """调用方持有拓扑锁；热应用与管理诊断共用同一份真实进程发现。"""
    async def gateway_status() -> RuntimeLoggingStatus:
        return control._validated_status(asdict(runtime_logging_state()), "gateway", os.getpid())

    async def gateway_apply(request: RuntimeLoggingApply) -> RuntimeLoggingStatus:
        return RuntimeLoggingStatus.model_validate(asdict(apply_runtime_logging(**request.model_dump())))

    processes = [RuntimeLoggingProcess("gateway", (), gateway_status, gateway_apply, lambda: True)]
    launcher = os.environ.get("BOXTEAM_LAUNCHER_CONTROL")
    if launcher is not None:
        endpoint = json.loads(launcher)
        if (not isinstance(endpoint, dict) or set(endpoint) != {"address", "pid"}
            or not isinstance(endpoint["address"], str) or type(endpoint["pid"]) is not int
            or endpoint["pid"] <= 0):
            raise ValueError("Launcher 交付的日志控制端点非法")
        address, pid = endpoint["address"], endpoint["pid"]
        processes.append(RuntimeLoggingProcess(
            "launcher", (), lambda: control.launcher_status(address=address, pid=pid),
            lambda request: control.launcher_apply(address=address, pid=pid, request=request), lambda: True,
        ))

    http_processes: dict[tuple[str, int], RuntimeLoggingProcess] = {}

    def add_http(handle: ManagedProcessHandle, service: RuntimeLoggingHttpService, url: str, workspace_ids: tuple[str, ...]) -> None:
        key = (service, handle.pid)
        if key in http_processes:
            process = http_processes[key]
            process.workspace_ids = tuple(dict.fromkeys((*process.workspace_ids, *workspace_ids)))
            return
        process = RuntimeLoggingProcess(
            service, workspace_ids,
            lambda: control.status(base_url=url, service=service, pid=handle.pid),
            lambda request: control.apply(base_url=url, service=service, pid=handle.pid, request=request),
            handle.is_alive,
        )
        http_processes[key] = process
        processes.append(process)

    for owner in registry.backend_owners():
        if owner.process is not None and owner.process.is_alive():
            add_http(owner.process, "backend", owner.contract.backend_url, owner.contract.workspace_ids)
    for target in registry.targets():
        if target.connection_kind != "local" or not target.managed or not registry.has_runtime(target.workspace_id):
            continue
        runtime = registry.managed_runtime(target.workspace_id)
        roles: dict[str, RuntimeLoggingHttpService] = {"terminal_manager": "terminal", "browser_manager": "browser"}
        for name, role in roles.items():
            handle = runtime.processes.get(name)
            if handle is not None and handle.is_alive():
                add_http(handle, role, registry.runtime_service_urls(target.workspace_id)[name], (target.workspace_id,))
    return processes


class RuntimeLoggingProcessControl:
    def __init__(self, *, client: httpx.AsyncClient, token: str) -> None:
        if not token:
            raise ValueError("日志进程控制缺少本机凭据")
        self._client = client
        self._token = token

    @staticmethod
    def _url(base_url: str, service: RuntimeLoggingHttpService) -> str:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.port is None or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("日志进程控制只接受带端口的本机 HTTP 服务根地址")
        return f"{base_url.rstrip('/')}{_CONTROL_PATHS[service]}"

    @staticmethod
    def _status(response: httpx.Response, service: RuntimeLoggingHttpService, pid: int) -> RuntimeLoggingStatus:
        response.raise_for_status()
        envelope = response.json()
        if not isinstance(envelope, dict) or "data" not in envelope:
            raise ValueError("日志进程控制响应缺少状态")
        return RuntimeLoggingProcessControl._validated_status(envelope["data"], service, pid)

    @staticmethod
    def _validated_status(payload: object, service: RuntimeLoggingService, pid: int) -> RuntimeLoggingStatus:
        state = RuntimeLoggingStatus.model_validate(payload)
        if state.service != service or state.pid != pid:
            raise RuntimeLoggingConflictError("日志进程身份与受管实例不匹配")
        return state

    @staticmethod
    def _confirmed_apply(state: RuntimeLoggingStatus, request: RuntimeLoggingApply) -> RuntimeLoggingStatus:
        if (state.runtime_instance_id != request.runtime_instance_id
            or state.applied_revision != request.revision
            or state.level != request.level or state.pretty != request.pretty):
            raise RuntimeLoggingConflictError("日志应用响应未确认请求的运行实例、revision 与实际设置")
        return state

    async def status(
        self, *, base_url: str, service: RuntimeLoggingHttpService, pid: int,
    ) -> RuntimeLoggingStatus:
        response = await self._client.get(
            self._url(base_url, service), headers={"X-Local-Token": self._token},
            follow_redirects=False, timeout=5,
        )
        return self._status(response, service, pid)

    async def apply(
        self, *, base_url: str, service: RuntimeLoggingHttpService, pid: int, request: RuntimeLoggingApply,
    ) -> RuntimeLoggingStatus:
        response = await self._client.post(
            self._url(base_url, service), headers={"X-Local-Token": self._token},
            json=request.model_dump(mode="json"), follow_redirects=False, timeout=5,
        )
        state = self._status(response, service, pid)
        return self._confirmed_apply(state, request)

    async def launcher_status(self, *, address: str, pid: int, timeout_seconds: float = 5) -> RuntimeLoggingStatus:
        payload = await self._launcher_request(address, {"action": "status"}, timeout_seconds)
        return self._validated_status(payload, "launcher", pid)

    async def launcher_apply(
        self, *, address: str, pid: int, request: RuntimeLoggingApply, timeout_seconds: float = 5,
    ) -> RuntimeLoggingStatus:
        payload = await self._launcher_request(
            address, {"action": "apply", "payload": request.model_dump(mode="json")}, timeout_seconds,
        )
        return self._confirmed_apply(self._validated_status(payload, "launcher", pid), request)

    async def _launcher_request(
        self, address: str, payload: dict[str, object], timeout_seconds: float,
    ) -> object:
        """地址由 Launcher 显式交付；不复制 home 到 socket／命名管道的规则。"""
        async with asyncio.timeout(timeout_seconds):
            reader, writer = await _open_launcher_control(address)
            try:
                message = {"operation": "runtime_logging", "token": self._token, **payload}
                writer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
                line = await reader.readline()
                if not line.endswith(b"\n"):
                    raise RuntimeError("Launcher 日志控制连接在完整响应前结束")
                response = json.loads(line)
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise ValueError("Launcher 日志控制响应缺少明确结果")
                if not response["ok"]:
                    raise RuntimeError(f"Launcher 日志控制拒绝: {response.get('error')}")
                if "data" not in response:
                    raise ValueError("Launcher 日志控制响应缺少状态")
                return response["data"]
            finally:
                writer.close()
                await writer.wait_closed()


async def _open_launcher_control(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if sys.platform == "win32":
        from asyncio.windows_events import ProactorEventLoop

        if not address.startswith("\\\\.\\pipe\\"):
            raise ValueError("Launcher 日志控制要求本机命名管道")
        loop = asyncio.get_running_loop()
        if not isinstance(loop, ProactorEventLoop):
            raise RuntimeError("Launcher 命名管道控制需要 Windows Proactor 事件循环")
        reader = asyncio.StreamReader(limit=64 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.create_pipe_connection(lambda: protocol, address)
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
    if not address.startswith("/"):
        raise ValueError("Launcher 日志控制要求显式绝对 Unix socket 地址")
    return await asyncio.open_unix_connection(address, limit=64 * 1024)
