from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.ssh_config import resolve_user_ssh_host
from app.gateway.ssh_connections import list_ssh_connection_options
from app.schemas.gateway.port_forwarding import PortForwardHostDTO


@dataclass(frozen=True, slots=True)
class SshHost:
    host: str
    port: int
    username: str
    private_key_path: Path | None
    ssh_config_host: str | None


def list_hosts(registry: GatewayWorkspaceRegistry) -> list[PortForwardHostDTO]:
    return [PortForwardHostDTO(
        host_id="local", label=socket.gethostname(), description="Gateway", kind="local",
    ), *(PortForwardHostDTO(
        host_id=option.connection_id, label=option.label,
        description=f"{option.username}@{option.host}:{option.port}", kind="ssh",
    ) for option in list_ssh_connection_options(registry))]


def resolve_host(registry: GatewayWorkspaceRegistry, host_id: str) -> SshHost | None:
    if host_id == "local":
        return None
    if host_id.startswith("ssh-config:"):
        host = resolve_user_ssh_host(host_id.removeprefix("ssh-config:"))
        return SshHost(host.hostname, host.port, host.username, None, host.alias)
    connection = registry.remote_gateway_connection(host_id)
    return SshHost(
        connection.host, connection.port, connection.username,
        Path(connection.private_key_path) if connection.private_key_path else None,
        connection.ssh_config_host,
    )
