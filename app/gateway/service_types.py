from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


GatewayServiceName = Literal[
    "workspace_api",
    "terminal_manager",
    "browser_manager",
]


@dataclass(frozen=True, slots=True)
class RemoteServiceSpec:
    name: GatewayServiceName
    host: str
    port: int
    required: bool = False


@dataclass(frozen=True, slots=True)
class LocalForwardSpec:
    name: GatewayServiceName
    local_port: int
    remote_host: str
    remote_port: int

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"


def default_remote_services(
    *,
    backend_host: str,
    backend_port: int,
) -> tuple[RemoteServiceSpec, ...]:
    return (
        RemoteServiceSpec(
            name="workspace_api",
            host=backend_host,
            port=backend_port,
            required=True,
        ),
        RemoteServiceSpec(
            name="terminal_manager",
            host="127.0.0.1",
            port=8012,
        ),
        RemoteServiceSpec(
            name="browser_manager",
            host="127.0.0.1",
            port=8015,
        ),
    )
