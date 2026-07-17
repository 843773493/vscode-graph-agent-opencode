from __future__ import annotations

import posixpath
from pathlib import Path

from app.gateway.backend_probe import read_workspace_root
from app.gateway.config import resolve_gateway_path
from app.gateway.processes import (
    allocate_ssh_tunnel_port,
    start_ssh_tunnel_process,
    wait_for_http_ok,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    SshWorkspaceConnection,
    WorkspaceTarget,
)
from app.gateway.service_runtime import WorkspaceRuntime
from app.gateway.service_types import (
    LocalForwardSpec,
    RemoteServiceSpec,
    default_remote_services,
)
from app.gateway.workspace_ids import build_ssh_workspace_id


async def register_ssh_workspace(
    *,
    registry: GatewayWorkspaceRegistry,
    log_dir: Path,
    name: str | None,
    host: str,
    port: int,
    username: str,
    private_key_path: str | None,
    ssh_config_host: str | None,
    remote_backend_host: str,
    remote_backend_port: int,
    remote_services: tuple[RemoteServiceSpec, ...] | None = None,
    remote_workspace_path: str,
    activate: bool,
    name_customized: bool = False,
) -> WorkspaceTarget:
    resolved_private_key_path = (
        resolve_gateway_path(private_key_path) if private_key_path else None
    )
    if resolved_private_key_path is not None and not resolved_private_key_path.is_file():
        raise FileNotFoundError(f"SSH 私钥不存在: {resolved_private_key_path}")
    if resolved_private_key_path is None and not ssh_config_host:
        raise ValueError("显式 SSH 连接必须提供 private_key_path")
    normalized_remote_workspace_path = remote_workspace_path.strip()
    if not normalized_remote_workspace_path:
        raise ValueError("remote_workspace_path 不能为空")

    resolved_services = remote_services or default_remote_services(
        backend_host=remote_backend_host,
        backend_port=remote_backend_port,
    )
    service_names = [service.name for service in resolved_services]
    if len(service_names) != len(set(service_names)):
        raise ValueError("远端服务定义包含重复 name")
    if "workspace_api" not in service_names:
        raise ValueError("远端服务定义缺少 workspace_api")
    allocated_ports: set[int] = set()
    forwards: list[LocalForwardSpec] = []
    for service in resolved_services:
        local_port = allocate_ssh_tunnel_port()
        while local_port in allocated_ports:
            local_port = allocate_ssh_tunnel_port()
        allocated_ports.add(local_port)
        forwards.append(
            LocalForwardSpec(
                name=service.name,
                local_port=local_port,
                remote_host=service.host,
                remote_port=service.port,
            )
        )
    service_urls = {forward.name: forward.local_url for forward in forwards}
    backend_url = service_urls["workspace_api"]
    tunnel = start_ssh_tunnel_process(
        host=host,
        port=port,
        username=username,
        private_key_path=resolved_private_key_path,
        ssh_config_host=ssh_config_host,
        forwards=tuple(forwards),
        log_dir=log_dir,
    )
    try:
        await wait_for_http_ok(f"{backend_url}/api/v1/health", tunnel.process)
        backend_workspace_root = await read_workspace_root(backend_url)
        if posixpath.normpath(backend_workspace_root) != posixpath.normpath(
            normalized_remote_workspace_path
        ):
            raise ValueError(
                "远程后端实际工作区与所选目录不一致: "
                f"后端={backend_workspace_root}, 所选={normalized_remote_workspace_path}。"
                "请为所选目录启动 BoxTeam 后端，或选择与该后端对应的目录。"
            )
    except Exception:
        tunnel.close()
        raise

    workspace_id = build_ssh_workspace_id(
        root_path=normalized_remote_workspace_path,
        host=host,
        port=port,
        username=username,
        remote_backend_host=remote_backend_host,
        remote_backend_port=remote_backend_port,
    )
    return registry.upsert(
        WorkspaceTarget(
            workspace_id=workspace_id,
            name=name or Path(normalized_remote_workspace_path).name or "remote",
            name_customized=name_customized,
            root_path=normalized_remote_workspace_path,
            backend_url=backend_url,
            connection_kind="ssh",
            managed=True,
            ssh_connection=SshWorkspaceConnection(
                host=host,
                port=port,
                username=username,
                private_key_path=(
                    str(resolved_private_key_path)
                    if resolved_private_key_path is not None
                    else None
                ),
                remote_backend_host=remote_backend_host,
                remote_backend_port=remote_backend_port,
                ssh_config_host=ssh_config_host,
                remote_services=resolved_services,
            ),
        ),
        runtime=WorkspaceRuntime(
            service_urls=service_urls,
            processes=[tunnel],
        ),
        activate=activate,
    )
