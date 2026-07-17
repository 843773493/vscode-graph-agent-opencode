from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core.env import get_project_root
from app.core.path_utils import get_gateway_root, get_user_workspace_root
from app.gateway.config import load_gateway_config
from app.gateway.local_workspace import start_managed_local_workspace_runtime
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.service_runtime import WorkspaceRuntime
from app.gateway.service_types import RemoteServiceSpec
from app.gateway.ssh_workspace import register_ssh_workspace
from app.gateway.workspace_ids import build_ssh_workspace_id, build_workspace_id


logger = logging.getLogger(__name__)


def _default_workspace_root() -> Path:
    configured_root = os.environ.get("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT")
    root_path = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else get_user_workspace_root()
    )
    root_path.mkdir(parents=True, exist_ok=True)
    return root_path


def _gateway_config_workspace_root(default_root_path: Path) -> Path:
    configured_root = os.environ.get("BOXTEAM_GATEWAY_CONFIG_WORKSPACE_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    raw_runtime_root = os.environ.get("WORKSPACE_ROOT")
    if raw_runtime_root:
        return Path(raw_runtime_root).expanduser().resolve()
    return default_root_path


async def _restore_managed_local_workspace(
    registry: GatewayWorkspaceRegistry,
    target: WorkspaceTarget,
) -> None:
    workspace_root = Path(target.root_path).expanduser().resolve()
    if not workspace_root.is_dir():
        registry.mark_connection_error(
            target.workspace_id,
            f"本地工作区目录不存在，无法恢复托管后端: {workspace_root}",
        )
        return
    runtime = await start_managed_local_workspace_runtime(
        project_root=get_project_root(),
        workspace_root=workspace_root,
        log_dir=get_gateway_root() / "logs",
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id=target.workspace_id,
            name=target.name,
            name_customized=target.name_customized,
            root_path=str(workspace_root),
            backend_url=runtime.service_urls["workspace_api"],
            connection_kind="local",
            managed=True,
            removable=target.removable,
            system_default=target.system_default,
        ),
        runtime=runtime,
        activate=False,
    )


async def create_registry() -> GatewayWorkspaceRegistry:
    gateway_root = get_gateway_root()
    registry = GatewayWorkspaceRegistry(storage_path=gateway_root / "workspaces.json")
    persisted_targets = registry.targets()
    persisted_targets_by_id = {
        target.workspace_id: target for target in persisted_targets
    }
    persisted_active_workspace_id = registry.active_workspace_id
    default_root_path = _default_workspace_root()
    default_backend_url = os.environ.get("BOXTEAM_DEFAULT_BACKEND_URL")
    default_workspace_id: str | None = None
    if default_backend_url:
        root_path = str(default_root_path)
        backend_url = default_backend_url.rstrip("/")
        default_workspace_id = build_workspace_id("local", root_path, backend_url)
        persisted_default = persisted_targets_by_id.get(default_workspace_id)
        registry.upsert(
            WorkspaceTarget(
                workspace_id=default_workspace_id,
                name=(
                    persisted_default.name
                    if (
                        persisted_default is not None
                        and persisted_default.name_customized
                    )
                    else os.environ.get("BOXTEAM_DEFAULT_WORKSPACE_NAME") or "home"
                ),
                name_customized=(
                    persisted_default.name_customized
                    if persisted_default is not None
                    else False
                ),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind="local",
                managed=False,
                removable=False,
                system_default=True,
            ),
            runtime=WorkspaceRuntime(
                service_urls={
                    "workspace_api": backend_url,
                    "terminal_manager": os.environ.get(
                        "BOXTEAM_TERMINAL_BACKEND_URL",
                        "http://127.0.0.1:8012",
                    ).rstrip("/"),
                    "browser_manager": os.environ.get(
                        "BOXTEAM_BROWSER_BACKEND_URL",
                        "http://127.0.0.1:8015",
                    ).rstrip("/"),
                }
            ),
            activate=persisted_active_workspace_id is None,
        )
        registry.remove_backend_aliases(
            backend_url=backend_url,
            keep_workspace_id=default_workspace_id,
        )

    gateway_config = load_gateway_config(
        _gateway_config_workspace_root(default_root_path)
    )
    configured_workspace_ids: set[str] = set()
    configured_active_workspace_id: str | None = None
    for configured_workspace in gateway_config.workspaces:
        workspace_id = build_ssh_workspace_id(
            root_path=configured_workspace.remote_workspace_path,
            host=configured_workspace.host,
            port=configured_workspace.port,
            username=configured_workspace.username,
            remote_backend_host=configured_workspace.remote_backend_host,
            remote_backend_port=configured_workspace.remote_backend_port,
        )
        configured_workspace_ids.add(workspace_id)
        persisted_configured_workspace = persisted_targets_by_id.get(workspace_id)
        await register_ssh_workspace(
            registry=registry,
            log_dir=gateway_root / "logs",
            name=(
                persisted_configured_workspace.name
                if (
                    persisted_configured_workspace is not None
                    and persisted_configured_workspace.name_customized
                )
                else configured_workspace.name
            ),
            host=configured_workspace.host,
            port=configured_workspace.port,
            username=configured_workspace.username,
            private_key_path=configured_workspace.private_key_path,
            ssh_config_host=None,
            remote_backend_host=configured_workspace.remote_backend_host,
            remote_backend_port=configured_workspace.remote_backend_port,
            remote_services=(
                RemoteServiceSpec(
                    name="workspace_api",
                    host=configured_workspace.remote_backend_host,
                    port=configured_workspace.remote_backend_port,
                    required=True,
                ),
                RemoteServiceSpec(
                    name="terminal_manager",
                    host=configured_workspace.remote_terminal_backend_host,
                    port=configured_workspace.remote_terminal_backend_port,
                ),
                RemoteServiceSpec(
                    name="browser_manager",
                    host=configured_workspace.remote_browser_backend_host,
                    port=configured_workspace.remote_browser_backend_port,
                ),
            ),
            remote_workspace_path=configured_workspace.remote_workspace_path,
            activate=configured_workspace.activate,
            name_customized=(
                persisted_configured_workspace.name_customized
                if persisted_configured_workspace is not None
                else False
            ),
        )
        if configured_workspace.activate:
            configured_active_workspace_id = workspace_id

    for target in persisted_targets:
        if target.connection_kind != "local" or not target.managed or target.system_default:
            continue
        try:
            await _restore_managed_local_workspace(registry, target)
        except Exception as error:
            message = (
                f"恢复本地托管工作区失败: workspace_id={target.workspace_id}: {error}"
            )
            registry.mark_connection_error(target.workspace_id, message)
            logger.exception(message)

    for target in persisted_targets:
        if target.connection_kind != "ssh" or target.workspace_id in configured_workspace_ids:
            continue
        connection = target.ssh_connection
        if connection is None:
            registry.mark_connection_error(
                target.workspace_id,
                "持久化记录缺少 SSH 重连信息，无法恢复 SSH 隧道，请删除后重新添加",
            )
            continue
        try:
            await register_ssh_workspace(
                registry=registry,
                log_dir=gateway_root / "logs",
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
        except Exception as error:
            message = f"恢复 SSH 工作区失败: workspace_id={target.workspace_id}: {error}"
            registry.mark_connection_error(target.workspace_id, message)
            logger.exception(message)

    requested_active_workspace_id = (
        configured_active_workspace_id
        or persisted_active_workspace_id
        or default_workspace_id
    )
    if requested_active_workspace_id and registry.has_target(requested_active_workspace_id):
        registry.activate(requested_active_workspace_id)
    elif default_workspace_id:
        registry.activate(default_workspace_id)
    registry.ensure_default_workspace_first()
    return registry
