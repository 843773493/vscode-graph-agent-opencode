from __future__ import annotations

from dataclasses import dataclass

from app.gateway.registry import GatewayWorkspaceRegistry, SshWorkspaceConnection
from app.gateway.schemas import (
    AddSshWorkspaceRequest,
    SshConnectionOptionDTO,
)
from app.gateway.ssh_config import list_user_ssh_hosts, resolve_user_ssh_host
from app.gateway.service_types import RemoteServiceSpec, default_remote_services


@dataclass(frozen=True, slots=True)
class ResolvedSshConnection:
    host: str
    port: int
    username: str
    private_key_path: str | None
    ssh_config_host: str | None
    remote_backend_host: str
    remote_backend_port: int
    remote_services: tuple[RemoteServiceSpec, ...]


def list_ssh_connection_options(
    registry: GatewayWorkspaceRegistry,
) -> list[SshConnectionOptionDTO]:
    options: list[SshConnectionOptionDTO] = []
    seen_boxteam_connections: set[tuple[str, int, str, str | None]] = set()
    for target in registry.targets():
        connection = target.ssh_connection
        if target.connection_kind != "ssh" or connection is None:
            continue
        signature = (
            connection.host,
            connection.port,
            connection.username,
            connection.ssh_config_host,
        )
        if signature in seen_boxteam_connections:
            continue
        seen_boxteam_connections.add(signature)
        options.append(
            SshConnectionOptionDTO(
                connection_id=f"boxteam:{target.workspace_id}",
                source="boxteam",
                label=target.name,
                host=connection.host,
                port=connection.port,
                username=connection.username,
                workspace_id=target.workspace_id,
                ssh_config_host=connection.ssh_config_host,
                initial_path=target.root_path,
            )
        )
    for host in list_user_ssh_hosts():
        options.append(
            SshConnectionOptionDTO(
                connection_id=f"ssh-config:{host.alias}",
                source="ssh_config",
                label=host.alias,
                host=host.hostname,
                port=host.port,
                username=host.username,
                ssh_config_host=host.alias,
            )
        )
    return options


def resolve_ssh_connection_request(
    payload: AddSshWorkspaceRequest,
    registry: GatewayWorkspaceRegistry,
) -> ResolvedSshConnection:
    if payload.connection_workspace_id:
        connection = registry.resolve_ssh_connection(payload.connection_workspace_id)
        return ResolvedSshConnection(
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=connection.private_key_path,
            ssh_config_host=connection.ssh_config_host,
            remote_backend_host=connection.remote_backend_host,
            remote_backend_port=connection.remote_backend_port,
            remote_services=(
                connection.remote_services
                or default_remote_services(
                    backend_host=connection.remote_backend_host,
                    backend_port=connection.remote_backend_port,
                )
            ),
        )
    if payload.ssh_config_host:
        host = resolve_user_ssh_host(payload.ssh_config_host)
        return ResolvedSshConnection(
            host=host.hostname,
            port=host.port,
            username=host.username,
            private_key_path=None,
            ssh_config_host=host.alias,
            remote_backend_host="127.0.0.1",
            remote_backend_port=8010,
            remote_services=default_remote_services(
                backend_host="127.0.0.1",
                backend_port=8010,
            ),
        )
    raise RuntimeError("AddSshWorkspaceRequest 连接来源未通过校验")


def resolve_directory_connection(
    connection_id: str,
    registry: GatewayWorkspaceRegistry,
) -> SshWorkspaceConnection:
    source, separator, identifier = connection_id.partition(":")
    if not separator or not identifier:
        raise ValueError(f"SSH connection_id 非法: {connection_id}")
    if source == "boxteam":
        return registry.resolve_ssh_connection(identifier)
    if source == "ssh-config":
        host = resolve_user_ssh_host(identifier)
        return SshWorkspaceConnection(
            host=host.hostname,
            port=host.port,
            username=host.username,
            private_key_path=None,
            remote_backend_host="127.0.0.1",
            remote_backend_port=8010,
            ssh_config_host=host.alias,
            remote_services=default_remote_services(
                backend_host="127.0.0.1",
                backend_port=8010,
            ),
        )
    raise ValueError(f"不支持的 SSH connection_id 来源: {source}")
