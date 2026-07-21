from __future__ import annotations

from dataclasses import dataclass, field

from app.gateway.runtime.process import ManagedProcess
from app.gateway.service_types import GatewayServiceName


@dataclass(slots=True)
class WorkspaceRuntime:
    service_urls: dict[GatewayServiceName, str]
    processes: dict[str, ManagedProcess] = field(default_factory=dict)
    backend_debug_port: int | None = None

    def set_process(self, name: str, process: ManagedProcess) -> None:
        if name in self.processes:
            raise ValueError(f"工作区运行时服务已存在: {name}")
        self.processes[name] = process

    def close_process(self, name: str) -> None:
        process = self.processes.pop(name, None)
        if process is not None:
            process.close()

    def close(self) -> None:
        errors: list[str] = []
        for name in reversed(tuple(self.processes)):
            try:
                self.close_process(name)
            except Exception as error:
                errors.append(f"{name}: {error}")
        self.processes.clear()
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))
