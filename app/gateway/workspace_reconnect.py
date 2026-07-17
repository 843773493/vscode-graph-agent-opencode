from __future__ import annotations

from pathlib import Path

from app.gateway.local_workspace import start_managed_local_workspace_runtime
from app.gateway.processes import wait_for_http_ok
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.service_runtime import WorkspaceRuntime
from app.gateway.ssh_workspace import register_ssh_workspace


async def reconnect_gateway_workspace(
    *,
    registry: GatewayWorkspaceRegistry,
    workspace_id: str,
    project_root: Path,
    log_dir: Path,
) -> None:
    """按注册信息重建工作区运行时，不改变稳定的 workspace_id。"""
    target = registry.resolve(workspace_id)

    if target.connection_kind == "ssh":
        connection = target.ssh_connection
        if connection is None:
            raise RuntimeError(f"SSH 工作区缺少重连信息: {workspace_id}")
        await register_ssh_workspace(
            registry=registry,
            log_dir=log_dir,
            name=target.name,
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=connection.private_key_path,
            ssh_config_host=connection.ssh_config_host,
            remote_backend_host=connection.remote_backend_host,
            remote_backend_port=connection.remote_backend_port,
            remote_services=connection.remote_services,
            remote_workspace_path=target.root_path,
            activate=False,
            name_customized=target.name_customized,
        )
        return

    if target.managed:
        runtime = await start_managed_local_workspace_runtime(
            project_root=project_root,
            workspace_root=Path(target.root_path),
            log_dir=log_dir,
        )
        target.backend_url = runtime.service_urls["workspace_api"]
    else:
        await wait_for_http_ok(f"{target.backend_url.rstrip('/')}/api/v1/health")
        runtime = WorkspaceRuntime(service_urls={"workspace_api": target.backend_url})

    target.connection_error = None
    registry.upsert(target, runtime=runtime, activate=False)
