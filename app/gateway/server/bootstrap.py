from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

from app.core.backend_identity import validate_backend_id
from app.core.env import get_project_root
from app.core.failures import failure_detail
from app.core.logging_contract import get_runtime_log_root
from app.core.path_utils import get_gateway_root, get_user_workspace_root
from app.core.shielded_cleanup import run_sync_shielded
from app.gateway.config import (
    ConfiguredRemoteGateway,
    GatewayConfig,
    load_gateway_config,
    resolve_gateway_path,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation import build_remote_gateway_connection_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceKey, WorkspaceTarget
from app.gateway.remote_gateway import (
    reconcile_configured_remote_gateways,
    reconnect_remote_gateway,
)
from app.gateway.runtime.local_workspace import (
    start_managed_backend_runtime,
    wait_for_workspace_health,
)
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
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


def _default_backend_port() -> int | None:
    raw_value = os.environ.get("BOXTEAM_DEFAULT_BACKEND_PORT")
    if raw_value is None or raw_value.strip() == "":
        return None
    value = int(raw_value)
    if value < 1 or value > 65535:
        raise ValueError(f"BOXTEAM_DEFAULT_BACKEND_PORT 必须是 1-65535: {raw_value}")
    return value


def _external_default_backend_url() -> str | None:
    configured_url = os.environ.get("BOXTEAM_DEFAULT_BACKEND_URL")
    if configured_url is None or configured_url.strip() == "":
        return None
    normalized_url = configured_url.strip().rstrip("/")
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
    return root_path


def _configured_remote_connections_unchanged(
    registry: GatewayWorkspaceRegistry,
    configured: dict[str, ConfiguredRemoteGateway],
) -> bool:
    persisted = {
        connection.connection_id: connection
        for connection in registry.remote_gateway_connections()
        if connection.source_owner == "config"
    }
    if not configured or configured.keys() != persisted.keys():
        return False
    return all(
        item.enabled and persisted[connection_id] == replace(
            persisted[connection_id],
            name=item.name or item.host,
            host=item.host,
            port=item.port,
            username=item.username,
            private_key_path=(str(resolve_gateway_path(item.private_key_path)) if item.private_key_path else None),
            ssh_config_host=item.ssh_config_host,
            remote_pair_command=item.remote_pair_command,
            remote_gateway_port=item.remote_gateway_port,
        )
        for connection_id, item in configured.items()
    )


async def _restore_managed_local_runtimes(
    *, registry: GatewayWorkspaceRegistry, default_workspace_id: str,
    gateway_config: GatewayConfig | None = None, only_workspace_ids: set[str] | None = None,
    exclude_workspace_ids: set[str] | None = None, preserve_existing_managed_runtimes: bool = False,
) -> None:
    resolved = gateway_config or load_gateway_config()
    await asyncio.to_thread(registry.validate_local_root_bindings, get_project_root())
    default = registry.resolve(default_workspace_id)
    if (not default.managed and (only_workspace_ids is None or default_workspace_id in only_workspace_ids)
            and (exclude_workspace_ids is None or default_workspace_id not in exclude_workspace_ids)):
        health = await wait_for_workspace_health(
            f"{default.backend_url}/api/v1/health", default_workspace_id,
            request_timeout_seconds=resolved.gateway_process_health_request_timeout_seconds,
            poll_interval_seconds=resolved.gateway_process_health_poll_interval_seconds,
        )
        backend_id = validate_backend_id(health.get("backend_id"))
        if default.backend_id is not None and default.backend_id != backend_id:
            raise ValueError("外部默认 Backend 安装域与原登记不匹配，不能隐式改绑")
        default.backend_id = backend_id
    for owner in registry.backend_owners():
        ids = set(owner.contract.workspace_ids)
        if (only_workspace_ids is not None and not ids.intersection(only_workspace_ids)) or (
            exclude_workspace_ids is not None and ids.intersection(exclude_workspace_ids)
        ) or not all(registry.resolve(member_id).desired_running for member_id in ids):
            continue
        async with registry.backend_topology_lock, owner.lock:
            if owner.process is not None and owner.process.is_alive() and owner.error is None:
                continue
            contract: ManagedBackendContract | None = None
            try:
                await run_sync_shielded(owner.close)
                reusable = {member_id: registry.managed_runtime(member_id) for member_id in ids if registry.has_runtime(member_id)}
                contract, runtimes = await start_managed_backend_runtime(
                    owner=owner, registry=registry, project_root=get_project_root(), log_dir=get_runtime_log_root(),
                    reusable_runtimes=reusable,
                    preferred_backend_port=_default_backend_port() if default_workspace_id in ids else None,
                    adopt_existing_backend=preserve_existing_managed_runtimes,
                    health_request_timeout_seconds=resolved.gateway_process_health_request_timeout_seconds,
                    health_poll_interval_seconds=resolved.gateway_process_health_poll_interval_seconds,
                    connection_drain_timeout_seconds=resolved.gateway_process_connection_drain_timeout_seconds,
                    default_skill_groups=resolved.default_workspace_skill_groups,
                    preserve_adopted_processes_on_failure=preserve_existing_managed_runtimes,
                )
                registry.publish_backend_runtime(owner, contract, runtimes)
            except Exception as error:
                if contract is not None and owner.contract is contract:
                    raise
                registry.mark_backend_error(owner, failure_detail(
                    error, stage="backend_restore",
                    label=f"Gateway 启动恢复完整 Backend 失败: owner={owner.contract.owner_id}",
                ))
                if default_workspace_id in ids:
                    raise
                if registry.active_workspace in {(registry.gateway_id, member_id) for member_id in ids} and registry.has_target(default_workspace_id):
                    registry.activate(default_workspace_id)
                    logger.warning("Gateway 完整组恢复失败，已回退到默认工作区: owner=%s", owner.contract.owner_id)



async def create_registry(
    gateway_config: GatewayConfig | None = None,
    state_store: GatewayStateStore | None = None,
    preserve_existing_managed_runtimes: bool = False,
    runtime_config_provider: Callable[[], GatewayConfig] | None = None,
) -> GatewayWorkspaceRegistry:
    gateway_root = get_gateway_root()
    resolved_gateway_config = gateway_config or load_gateway_config()

    def current_logging_config() -> dict[str, object]:
        current = runtime_config_provider() if runtime_config_provider is not None else resolved_gateway_config
        settings = cast(dict[str, object], cast(dict[str, object], current.payload["logging"])["runtime"])
        return {"revision": current.revision, "level": settings["level"], "pretty": settings["pretty"]}

    registry = GatewayWorkspaceRegistry(
        storage_path=gateway_root / "workspaces.json",
        state_store=state_store,
        runtime_logging_config=current_logging_config,
    )
    persisted_targets = registry.targets()
    persisted_targets_by_id = {
        target.key: target for target in persisted_targets
    }
    persisted_active_workspace = registry.active_workspace
    await asyncio.to_thread(registry.validate_local_root_bindings, get_project_root())
    default_root_path = _default_workspace_root()
    root_path = str(default_root_path)
    default_workspace_id = await asyncio.to_thread(
        registry.resolve_local_workspace_identity, get_project_root(), default_root_path, create_directory=True,
    )
    persisted_default = persisted_targets_by_id.get((registry.gateway_id, default_workspace_id))
    if persisted_default is None:
        persisted_default = next(
            (
                target
                for target in persisted_targets
                if target.system_default and target.root_path == root_path
            ),
            None,
        )
    try:
        return await _complete_registry_startup(
            resolved_gateway_config=resolved_gateway_config,
            registry=registry,
            persisted_default=persisted_default,
            persisted_active_workspace=persisted_active_workspace,
            default_root_path=default_root_path,
            root_path=root_path,
            default_workspace_id=default_workspace_id,
            preserve_existing_managed_runtimes=preserve_existing_managed_runtimes,
        )
    except BaseException as startup_error:
        try:
            await run_sync_shielded(
                registry.close, detach_adopted_managed_processes=preserve_existing_managed_runtimes,
            )
        except BaseException as cleanup_error:  # noqa: BLE001 - 取消与清理错误必须完整保留
            raise BaseExceptionGroup(
                "Gateway registry 构造失败且已创建运行时清理失败",
                [startup_error, cleanup_error],
            ) from None
        raise


async def _complete_registry_startup(
    *,
    resolved_gateway_config: GatewayConfig,
    registry: GatewayWorkspaceRegistry,
    persisted_default: WorkspaceTarget | None,
    persisted_active_workspace: WorkspaceKey | None,
    default_root_path: Path,
    root_path: str,
    default_workspace_id: str,
    preserve_existing_managed_runtimes: bool,
) -> GatewayWorkspaceRegistry:
    external_backend_url = _external_default_backend_url()
    owner = registry.backend_owner(default_workspace_id) if registry.has_target(default_workspace_id) else None
    if external_backend_url is not None and owner is not None:
        if len(owner.contract.members) != 1:
            raise ValueError("外部默认后端不能替换共享 managed Backend 组")
        registry.discard_stopped_backend_owner(owner)
        owner = None
    registry.upsert(
        WorkspaceTarget(
            gateway_id=registry.gateway_id,
            workspace_id=default_workspace_id,
            name=persisted_default.name if persisted_default is not None and persisted_default.name_customized else os.environ.get("BOXTEAM_DEFAULT_WORKSPACE_NAME") or "home",
            name_customized=persisted_default.name_customized if persisted_default is not None else False,
            root_path=root_path, backend_url=external_backend_url or (owner.contract.backend_url if owner is not None else ""),
            connection_kind="local", owner="system",
            managed=external_backend_url is None, removable=False, system_default=True,
            desired_running=external_backend_url is None,
            local_service_urls=persisted_default.local_service_urls if persisted_default is not None else {},
        ), activate=persisted_active_workspace is None,
    )
    if external_backend_url is None and owner is None:
        owner = ManagedBackendOwner(ManagedBackendContract(
            gateway_id=registry.gateway_id, owner_id=f"backend_owner_{uuid4().hex}", generation=f"backend_generation_{uuid4().hex}",
            members=(ManagedBackendMember(workspace_id=default_workspace_id),),
            backend_url=persisted_default.backend_url if persisted_default is not None else "",
            debug_port=_default_backend_debug_port(),
        ))
        registry.register_backend_owner(owner)
    registry.remove_system_default_aliases(
        keep_workspace_id=default_workspace_id,
    )
    configured_active_workspace: WorkspaceKey | None = None
    configured_connections = {
        (item.connection_id or build_remote_gateway_connection_id(
            host=item.host,
            port=item.port,
            username=item.username,
            remote_gateway_port=item.remote_gateway_port,
        )): item
        for item in resolved_gateway_config.workspaces
    }
    # 普通冷启动复用原连接恢复链；候选与配置增删改仍须完整 batch 验证。
    restore_configured_connections = (
        not preserve_existing_managed_runtimes
        and len(configured_connections) == len(resolved_gateway_config.workspaces)
        and _configured_remote_connections_unchanged(registry, configured_connections)
    )
    if not restore_configured_connections:
        await reconcile_configured_remote_gateways(
            registry=registry,
            configured_workspaces=resolved_gateway_config.workspaces,
            log_dir=get_runtime_log_root(),
            health_request_timeout_seconds=(
                resolved_gateway_config.gateway_process_health_request_timeout_seconds
            ),
            health_poll_interval_seconds=(
                resolved_gateway_config.gateway_process_health_poll_interval_seconds
            ),
        )
    configured_active_connection_ids = {
        connection_id for connection_id, item in configured_connections.items()
        if item.activate and not restore_configured_connections
    }
    for target in registry.targets():
        if set(target.remote_gateway_connection_ids) & configured_active_connection_ids:
            configured_active_workspace = target.key
            break

    for connection in registry.remote_gateway_connections():
        if connection.connection_id in configured_connections and not restore_configured_connections:
            continue
        try:
            await reconnect_remote_gateway(
                registry=registry,
                connection_id=connection.connection_id,
                log_dir=get_runtime_log_root(),
                health_request_timeout_seconds=(
                    resolved_gateway_config.gateway_process_health_request_timeout_seconds
                ),
                health_poll_interval_seconds=(
                    resolved_gateway_config.gateway_process_health_poll_interval_seconds
                ),
            )
        except Exception as error:  # noqa: BLE001 — 独立记录各远端恢复失败，继续其它连接。
            message = failure_detail(
                error, stage="remote_gateway_restore",
                label=f"恢复远程 Gateway 失败: connection_id={connection.connection_id}",
            )
            for target in registry.targets():
                if connection.connection_id in target.remote_gateway_connection_ids:
                    registry.mark_connection_error(target.key, message)

    requested_active_workspace_id = (
        configured_active_workspace
        or persisted_active_workspace
        or (registry.gateway_id, default_workspace_id)
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
                # 上次激活的托管工作区已被标记为不期望运行，启动时无法恢复它，
                # 必须回退到默认工作区；该回退用户可见，不能静默发生。
                registry.activate(default_workspace_id)
                logger.warning(
                    "Gateway 启动时上次激活的托管工作区不期望运行，已回退到默认工作区: "
                    "requested_workspace_id=%s, fallback_workspace_id=%s",
                    requested_active_workspace_id,
                    default_workspace_id,
                )
        else:
            registry.activate(requested_active_workspace_id)
    elif default_workspace_id:
        registry.activate(default_workspace_id)
    registry.ensure_default_workspace_first()
    return registry
