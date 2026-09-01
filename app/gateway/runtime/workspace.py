from __future__ import annotations

from dataclasses import dataclass, field

from app.gateway.runtime.process import ManagedProcessHandle
from app.gateway.service_types import GatewayServiceName


@dataclass(slots=True)
class WorkspaceRuntime:
    service_urls: dict[GatewayServiceName, str]
    processes: dict[str, ManagedProcessHandle] = field(default_factory=dict)
    backend_debug_port: int | None = None

    def set_process(self, name: str, process: ManagedProcessHandle) -> None:
        if name in self.processes:
            raise ValueError(f"工作区运行时服务已存在: {name}")
        self.processes[name] = process

    def close_process(self, name: str, *, timeout_seconds: float = 8) -> None:
        process = self.processes.pop(name, None)
        if process is not None:
            process.close(timeout_seconds=timeout_seconds)

    def detach_process(self, name: str) -> None:
        process = self.processes.pop(name, None)
        if process is not None:
            process.detach()

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
        for name in reversed(tuple(self.processes)):
            try:
                self.processes[name].request_terminate()
            except Exception as error:
                errors.append(f"{name} 发送终止信号失败: {error}")
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))

    def wait_closed(self) -> None:
        errors: list[str] = []
        for name in reversed(tuple(self.processes)):
            try:
                timeout_seconds = 120 if name == "browser_manager" else 8
                self.close_process(name, timeout_seconds=timeout_seconds)
            except Exception as error:
                errors.append(f"{name}: {error}")
        self.processes.clear()
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))

    def close(self) -> None:
        errors: list[str] = []
        try:
            self.request_terminate()
        except Exception as error:
            errors.append(str(error))
        try:
            self.wait_closed()
        except Exception as error:
            errors.append(str(error))
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))
