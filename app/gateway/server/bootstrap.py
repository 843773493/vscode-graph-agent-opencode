from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core.env import get_project_root
from app.core.path_utils import get_gateway_root, get_user_workspace_root
from app.gateway.config import GatewayConfig, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import build_remote_gateway_connection_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.remote_gateway import (
    reconnect_remote_gateway,
    register_remote_gateway,
)
from app.gateway.runtime.local_workspace import start_managed_local_workspace_runtime
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
            f"BOXTEAM_DEFAULT_BACKEND_DEBUG_PORT 必须是 1-65535: {raw_value}"
        )
    return value


def _external_default_backend_url() -> str | None:
    configured_url = os.environ.get("BOXTEAM_DEFAULT_BACKEND_URL")
    if configured_url is None or configured_url.strip() == "":
        return None
    normalized_url = configured_url.strip().rstrip("/")
    if normalized_url == "managed-by-gateway":
        return None
    if not normalized_url.startswith(("http://", "https://")):
        raise ValueError(
            "BOXTEAM_DEFAULT_BACKEND_URL 必须是 http:// 或 https:// 地址: "
            f"{configured_url}"
        )
    return normalized_url


def _default_workspace_root() -> Path:
    configured_root = os.environ.get("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT")
    root_path = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else get_user_workspace_root()
    )
    root_path.mkdir(parents=True, exist_ok=True)
    return root_path


async def _restore_managed_local_runtimes(
    *,
    registry: GatewayWorkspaceRegistry,
    default_workspace_id: str,
    gateway_root: Path,
    gateway_config: GatewayConfig | None = None,
    only_workspace_ids: set[str] | None = None,
    exclude_workspace_ids: set[str] | None = None,
) -> None:
    resolved_gateway_config = gateway_config or load_gateway_config()
    for persisted_target in registry.targets():
        if (
            (
                only_workspace_ids is not None
                and persisted_target.workspace_id not in only_workspace_ids
            )
            or (
                exclude_workspace_ids is not None
                and persisted_target.workspace_id in exclude_workspace_ids
            )
            or persisted_target.workspace_id == default_workspace_id
            or persisted_target.connection_kind != "local"
            or not persisted_target.managed
            or not persisted_target.desired_running
        ):
            continue
        target = registry.resolve(persisted_target.workspace_id)
        # 没有运行时的目标不会通过 resolve_service_url 转发；保留地址用于
        # 校验 Gateway 重启后仍存活的旧后端，并在瞬时失败时继续重试接管。
        reusable_backend_url = target.backend_url or None
        target.connection_error = "Gateway 正在恢复工作区运行时"
        registry.upsert(target, activate=False)
        workspace_root = Path(target.root_path).expanduser().resolve()
        try:
            if not workspace_root.is_dir():
                raise FileNotFoundError(f"工作区目录不存在: {workspace_root}")
            runtime = await start_managed_local_workspace_runtime(
                project_root=get_project_root(),
                workspace_root=workspace_root,
                log_dir=gateway_root / "logs",
                reusable_backend_url=reusable_backend_url,
                adopt_existing_backend=False,
                reusable_service_urls=target.local_service_urls,
                health_request_timeout_seconds=(
                    resolved_gateway_config.gateway_process_health_request_timeout_seconds
                ),
                health_poll_interval_seconds=(
                    resolved_gateway_config.gateway_process_health_poll_interval_seconds
                ),
                connection_drain_timeout_seconds=(
                    resolved_gateway_config.gateway_process_connection_drain_timeout_seconds
                ),
                default_skill_groups=resolved_gateway_config.default_workspace_skill_groups,
            )
        except Exception as error:
            target.connection_error = (
                "Gateway 启动时恢复托管工作区失败: "
                f"workspace_id={target.workspace_id}: {error}"
            )
            registry.upsert(target, activate=False)
            logger.exception(target.connection_error)
            continue
        target.backend_url = runtime.service_urls["workspace_api"]
        target.local_service_urls = {
            "terminal_manager": runtime.service_urls["terminal_manager"],
            "browser_manager": runtime.service_urls["browser_manager"]
        }
        target.connection_error = None
        registry.upsert(target, runtime=runtime, activate=False)


async def create_registry(
    gateway_config: GatewayConfig | None = None,
    state_store: GatewayStateStore | None = None,
) -> GatewayWorkspaceRegistry:
    gateway_root = get_gateway_root()
    resolved_gateway_config = gateway_config or load_gateway_config()
    registry = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json",
        state_store=state_store,
    )
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
    external_backend_url = _external_default_backend_url()
    default_runtime = None
    if external_backend_url is None:
        default_runtime = await start_managed_local_workspace_runtime(
            project_root=get_project_root(),
            workspace_root=default_root_path,
            log_dir=gateway_root / "logs",
            backend_debug_port=_default_backend_debug_port(),
            reusable_backend_url=(
                persisted_default.backend_url
                if persisted_default is not None and persisted_default.backend_url
                else None
            ),
            adopt_existing_backend=False,
            reusable_service_urls=(
                persisted_default.local_service_urls
                if persisted_default is not None
                else None
            ),
            health_request_timeout_seconds=(
                resolved_gateway_config.gateway_process_health_request_timeout_seconds
            ),
            health_poll_interval_seconds=(
                resolved_gateway_config.gateway_process_health_poll_interval_seconds
            ),
            connection_drain_timeout_seconds=(
                resolved_gateway_config.gateway_process_connection_drain_timeout_seconds
            ),
            default_skill_groups=resolved_gateway_config.default_workspace_skill_groups,
        )
        backend_url = default_runtime.service_urls["workspace_api"]
        local_service_urls = {
            "terminal_manager": default_runtime.service_urls["terminal_manager"],
            "browser_manager": default_runtime.service_urls["browser_manager"]
        }
        managed = True
    else:
        # 测试和外部开发编排可以显式提供已运行的工作区后端；Gateway 不应再为
        # 同一工作区启动第二个进程，否则会与工作区 SQLite 所有权锁冲突。
        backend_url = external_backend_url
        local_service_urls = {}
        managed = False
    registry.upsert(
        WorkspaceTarget(
            workspace_id=default_workspace_id,
            name=(
                persisted_default.name
                if (persisted_default is not None and persisted_default.name_customized)
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
            managed=managed,
            removable=False,
            system_default=True,
            local_service_urls=local_service_urls,
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
    configured_active_workspace_id: str | None = None
    for configured_workspace in resolved_gateway_config.workspaces:
        projected = await register_remote_gateway(
            registry=registry,
            log_dir=gateway_root / "logs",
            name=configured_workspace.name,
            host=configured_workspace.host,
            port=configured_workspace.port,
            username=configured_workspace.username,
            private_key_path=configured_workspace.private_key_path,
            ssh_config_host=configured_workspace.ssh_config_host,
            remote_gateway_port=configured_workspace.remote_gateway_port,
            remote_pair_command=configured_workspace.remote_pair_command,
            activate=configured_workspace.activate,
            health_request_timeout_seconds=(
                resolved_gateway_config.gateway_process_health_request_timeout_seconds
            ),
            health_poll_interval_seconds=(
                resolved_gateway_config.gateway_process_health_poll_interval_seconds
            ),
        )
        if configured_workspace.activate and projected:
            configured_active_workspace_id = projected[0].workspace_id

    configured_connection_ids = {
        build_remote_gateway_connection_id(
            host=item.host,
            port=item.port,
            username=item.username,
            remote_gateway_port=item.remote_gateway_port,
        )
            for item in resolved_gateway_config.workspaces
    }
    for connection in registry.remote_gateway_connections():
        if connection.connection_id in configured_connection_ids:
            continue
        try:
            await reconnect_remote_gateway(
                registry=registry,
                connection_id=connection.connection_id,
                log_dir=gateway_root / "logs",
                health_request_timeout_seconds=(
                    resolved_gateway_config.gateway_process_health_request_timeout_seconds
                ),
                health_poll_interval_seconds=(
                    resolved_gateway_config.gateway_process_health_poll_interval_seconds
                ),
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
    if requested_active_workspace_id and registry.has_target(
        requested_active_workspace_id
    ):
        requested_target = registry.resolve(requested_active_workspace_id)
        if (
            requested_target.connection_kind == "local"
            and requested_target.managed
            and not registry.has_runtime(requested_active_workspace_id)
        ):
            if requested_target.desired_running:
                # 托管工作区会在 Gateway 就绪后异步恢复；保留用户上次选择的
                # 工作区，避免冷启动时把会话树切回默认工作区。
                registry.activate(requested_active_workspace_id)
            else:
                registry.activate(default_workspace_id)
        else:
            registry.activate(requested_active_workspace_id)
    elif default_workspace_id:
        registry.activate(default_workspace_id)
    registry.ensure_default_workspace_first()
    return registry
