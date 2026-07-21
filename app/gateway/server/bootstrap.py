from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core.env import get_project_root
from app.core.path_utils import get_gateway_root, get_user_workspace_root
from app.gateway.config import load_gateway_config
from app.gateway.runtime.local_workspace import start_managed_local_workspace_runtime
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.federation import build_remote_gateway_connection_id
from app.gateway.remote_gateway import (
    reconnect_remote_gateway,
    register_remote_gateway,
)
from app.gateway.workspace_ids import (
    build_managed_local_workspace_id,
)


logger = logging.getLogger(__name__)


def _default_backend_debug_port() -> int | None:
    raw_value = os.environ.get("BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT")
    if raw_value is None or raw_value.strip() == "":
        return None
    value = int(raw_value)
    if value < 1 or value > 65535:
        raise ValueError(
            "BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT 必须是 1-65535: "
            f"{raw_value}"
        )
    return value


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
    root_path = str(default_root_path)
    default_workspace_id = build_managed_local_workspace_id(root_path)
    persisted_default = persisted_targets_by_id.get(default_workspace_id)
    if persisted_default is None:
        persisted_default = next(
            (
                target
                for target in persisted_targets
                if target.system_default and target.root_path == root_path
            ),
            None,
        )
    default_runtime = await start_managed_local_workspace_runtime(
        project_root=get_project_root(),
        workspace_root=default_root_path,
        log_dir=gateway_root / "logs",
        backend_debug_port=_default_backend_debug_port(),
    )
    backend_url = default_runtime.service_urls["workspace_api"]
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
            managed=True,
            removable=False,
            system_default=True,
        ),
        runtime=default_runtime,
        activate=persisted_active_workspace_id is None,
    )
    registry.remove_system_default_aliases(
        keep_workspace_id=default_workspace_id,
    )

    # TODO: Gateway 配置热重载需要先为 registry 目标增加 config/manual/system
    # 来源归属、原子 batch commit 与代理 runtime lease。否则删除配置可能误删手动
    # 目标，或在 HTTP/SSE/WebSocket 仍使用旧 SSH 隧道时提前关闭它。
    gateway_config = load_gateway_config(
        _gateway_config_workspace_root(default_root_path)
    )
    configured_active_workspace_id: str | None = None
    for configured_workspace in gateway_config.workspaces:
        projected = await register_remote_gateway(
            registry=registry,
            log_dir=gateway_root / "logs",
            name=configured_workspace.name,
            host=configured_workspace.host,
            port=configured_workspace.port,
            username=configured_workspace.username,
            private_key_path=configured_workspace.private_key_path,
            ssh_config_host=None,
            remote_gateway_port=configured_workspace.remote_gateway_port,
            activate=configured_workspace.activate,
        )
        if configured_workspace.activate and projected:
            configured_active_workspace_id = projected[0].workspace_id

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

    configured_connection_ids = {
        build_remote_gateway_connection_id(
            host=item.host,
            port=item.port,
            username=item.username,
            remote_gateway_port=item.remote_gateway_port,
        )
        for item in gateway_config.workspaces
    }
    for connection in registry.remote_gateway_connections():
        if connection.connection_id in configured_connection_ids:
            continue
        try:
            await reconnect_remote_gateway(
                registry=registry,
                connection_id=connection.connection_id,
                log_dir=gateway_root / "logs",
            )
        except Exception as error:
            message = (
                "恢复远程 Gateway 失败: "
                f"connection_id={connection.connection_id}: {error}"
            )
            for target in registry.targets():
                if target.remote_gateway_connection_id == connection.connection_id:
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
