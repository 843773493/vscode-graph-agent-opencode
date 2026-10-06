from __future__ import annotations

from dataclasses import dataclass, field

from app.gateway.runtime.process import ManagedProcessHandle
from app.gateway.service_types import GatewayServiceName


@dataclass(slots=True)
class WorkspaceRuntime:
    service_urls: dict[GatewayServiceName, str]
    processes: dict[str, ManagedProcessHandle] = field(default_factory=dict)
    adopted_process_names: set[str] = field(default_factory=set)
    backend_debug_port: int | None = None
    gateway_generation: str | None = None
    backend_workspace_id: str | None = None

    def set_process(
        self,
        name: str,
        process: ManagedProcessHandle,
        *,
        adopted: bool = False,
    ) -> None:
        if name in self.processes:
            raise ValueError(f"工作区运行时服务已存在: {name}")
        self.processes[name] = process
        if adopted:
            self.adopted_process_names.add(name)

    def replace_process(
        self,
        name: str,
        process: ManagedProcessHandle,
        *,
        adopted: bool = False,
    ) -> None:
        self.processes[name] = process
        self.adopted_process_names.discard(name)
        if adopted:
            self.adopted_process_names.add(name)

    def remove_process(self, name: str) -> None:
        self.processes.pop(name, None)
        self.adopted_process_names.discard(name)

    def assert_healthy(self) -> None:
        """确认运行时服务地址和 Gateway 所有的进程仍可服务。"""

        for service, url in self.service_urls.items():
            if not isinstance(url, str) or not url.strip():
                raise RuntimeError(f"运行时服务地址为空: service={service}")
        for name, process in self.processes.items():
            if not process.is_alive():
                raise RuntimeError(f"运行时进程已退出: process={name}")

    def close_process(self, name: str, *, timeout_seconds: float = 8) -> None:
        process = self.processes.get(name)
        if process is not None:
            process.close(timeout_seconds=timeout_seconds)
            self.remove_process(name)

    def detach_process(self, name: str) -> None:
        process = self.processes.get(name)
        if process is not None:
            process.detach()
            self.remove_process(name)

    def detach_adopted_processes(self) -> None:
        """候选 Gateway 启动失败时放弃旧 generation 的进程句柄。"""
        for name in tuple(self.processes):
            if name in self.adopted_process_names:
                self.detach_process(name)

    def close_for_gateway_restart(
        self,
        *,
        preserve_process_names: set[str] | None = None,
    ) -> None:
        """保留可接管的辅助服务；其它由 Gateway 直接拥有的进程正常关闭。"""
        names = {"browser_manager"}
        names.update(preserve_process_names or set())
        for name in names:
            self.detach_process(name)
        self.close()

    def request_terminate(self) -> None:
        errors: list[str] = []
        # 后端 unload 仍依赖辅助服务；先只通知后端，等其关闭后再终止依赖。
        names = ("workspace_api",) if "workspace_api" in self.processes else tuple(reversed(self.processes))
        for name in names:
            try:
                self.processes[name].request_terminate()
            except Exception as error:  # noqa: BLE001 - 排空所有 owned handle 后聚合并抛出错误
                errors.append(f"{name} 发送终止信号失败: {error}")
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))

    def wait_closed(self) -> None:
        # 后端失败时保留其句柄与全部依赖，重试仍从同一关闭边界开始。
        self.close_process("workspace_api")
        errors: list[str] = []
        for name in reversed(tuple(self.processes)):
            try:
                timeout_seconds = 120 if name == "browser_manager" else 8
                self.close_process(name, timeout_seconds=timeout_seconds)
            except Exception as error:  # noqa: BLE001 - 排空所有 owned handle 后聚合并抛出错误
                errors.append(f"{name}: {error}")
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))

    def close(self) -> None:
        errors: list[str] = []
        try:
            self.request_terminate()
        except Exception as error:  # noqa: BLE001 - 排空所有 owned handle 后聚合并抛出错误
            errors.append(str(error))
        try:
            self.wait_closed()
        except Exception as error:  # noqa: BLE001 - 排空所有 owned handle 后聚合并抛出错误
            errors.append(str(error))
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))
