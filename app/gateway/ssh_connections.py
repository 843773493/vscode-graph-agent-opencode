from __future__ import annotations

from dataclasses import dataclass

from app.gateway.config import load_gateway_config
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.gateway import (
    AddSshWorkspaceRequest,
    SshConnectionOptionDTO,
)
from app.gateway.ssh_config import (
    UserSshHost,
    list_user_ssh_hosts,
    resolve_user_ssh_host,
)


@dataclass(frozen=True, slots=True)
class ResolvedSshConnection:
    host: str
    port: int
    username: str
    private_key_path: str | None
    ssh_config_host: str | None
    remote_gateway_port: int
    remote_pair_command: str | None = None


def _configured_pairing_command(host: UserSshHost) -> str | None:
    """只从已安装的 Gateway 配置中读取目标专用配对命令。"""
    for configured in load_gateway_config().workspaces:
        if configured.ssh_config_host == host.alias:
            return configured.remote_pair_command
        if (
            configured.ssh_config_host is None
            and configured.host == host.hostname
            and configured.port == host.port
            and configured.username == host.username
        ):
            return configured.remote_pair_command
    return None


def list_ssh_connection_options(
    registry: GatewayWorkspaceRegistry,
) -> list[SshConnectionOptionDTO]:
    options: list[SshConnectionOptionDTO] = []
    seen_boxteam_connections: set[tuple[str, int, str, str | None]] = set()
    for target in registry.targets():
        if (
            target.connection_kind != "remote_gateway"
            or target.remote_gateway_connection_id is None
        ):
            continue
        connection = registry.remote_gateway_connection(
            target.remote_gateway_connection_id
        )
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
        target = registry.resolve(payload.connection_workspace_id)
        if (
            target.connection_kind != "remote_gateway"
            or target.remote_gateway_connection_id is None
        ):
            raise ValueError("所选工作区不属于远程 Gateway 连接")
        connection = registry.remote_gateway_connection(
            target.remote_gateway_connection_id
        )
        return ResolvedSshConnection(
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=connection.private_key_path,
            ssh_config_host=connection.ssh_config_host,
            remote_gateway_port=payload.remote_gateway_port,
            remote_pair_command=connection.remote_pair_command,
        )
    if payload.ssh_config_host:
        host = resolve_user_ssh_host(payload.ssh_config_host)
        return ResolvedSshConnection(
            host=host.hostname,
            port=host.port,
            username=host.username,
            private_key_path=None,
            ssh_config_host=host.alias,
            remote_gateway_port=payload.remote_gateway_port,
            remote_pair_command=_configured_pairing_command(host),
        )
    if payload.host and payload.username and payload.private_key_path:
        return ResolvedSshConnection(
            host=payload.host,
            port=payload.port,
            username=payload.username,
            private_key_path=payload.private_key_path,
            ssh_config_host=None,
            remote_gateway_port=payload.remote_gateway_port,
        )
    raise RuntimeError("AddSshWorkspaceRequest 连接来源未通过校验")
