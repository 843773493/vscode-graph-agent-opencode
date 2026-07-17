from __future__ import annotations

from dataclasses import dataclass, field

from app.gateway.processes import ManagedProcess
from app.gateway.service_types import GatewayServiceName


@dataclass(slots=True)
class WorkspaceRuntime:
    service_urls: dict[GatewayServiceName, str]
    processes: list[ManagedProcess] = field(default_factory=list)

    def close(self) -> None:
        errors: list[str] = []
        for process in reversed(self.processes):
            try:
                process.close()
            except Exception as error:
                errors.append(str(error))
        self.processes.clear()
        if errors:
            raise RuntimeError("关闭工作区运行时失败: " + "; ".join(errors))
