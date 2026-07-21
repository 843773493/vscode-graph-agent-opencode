from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


GatewayServiceName = Literal[
    "workspace_api",
    "terminal_manager",
    "browser_manager",
]


@dataclass(frozen=True, slots=True)
class LocalForwardSpec:
    name: GatewayServiceName
    local_port: int
    remote_host: str
    remote_port: int

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"
