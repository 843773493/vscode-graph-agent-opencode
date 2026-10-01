"""Gateway 路由与 lifespan 共享的辅助函数。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import (
    HTTPException,
    Request,
)

from app.core.path_utils import get_gateway_root
from app.gateway.config import GatewayConfigReloadService
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
)
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.managed_workspaces import list_direct_managed_workspaces
from app.gateway.registry import GatewayWorkspaceRegistry
from app.schemas.gateway import (
    GatewayDirectoryEntryDTO,
    GatewayDirectoryListDTO,
    GatewayManagedWorkspaceListDTO,
)

logger = logging.getLogger(__name__)


def _gateway_root() -> Path:
    return get_gateway_root()


async def _wait_for_managed_runtime_restore_tasks(restore_tasks: object) -> None:
    """等待托管 Workspace 恢复任务完成，并保留明确的超时状态。"""
    if not isinstance(restore_tasks, dict):
        return
    pending_tasks = {
        task
        for task in restore_tasks.values()
        if isinstance(task, asyncio.Task) and not task.done()
    }
    logger.info(
        "Gateway 等待托管 Workspace 恢复: task_count=%s, pending_count=%s",
        len(restore_tasks),
        len(pending_tasks),
    )
    for task in pending_tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=120.0)
        except TimeoutError:
            # 启动恢复超时不是隐藏错误；list_dtos 会返回 connection_error，
            # 让调用方看到工作区仍未就绪以及具体的恢复状态。
            return


def _resolve_local_directory(raw_path: str | None) -> Path:
    target_path = Path(raw_path).expanduser() if raw_path else Path.home()
    resolved_path = target_path.resolve()
    if not resolved_path.exists():
        raise HTTPException(status_code=400, detail=f"本机目录不存在: {resolved_path}")
    if not resolved_path.is_dir():
        raise HTTPException(status_code=400, detail=f"路径不是目录: {resolved_path}")
    return resolved_path


def _scan_local_directories(
    root_path: Path,
    limit: int,
) -> tuple[list[GatewayDirectoryEntryDTO], bool]:
    with os.scandir(root_path) as directory_iterator:
        directories = [
            entry for entry in directory_iterator if entry.is_dir(follow_symlinks=False)
        ]
    directories.sort(key=lambda entry: (entry.name.lower(), entry.name))
    entries = [
        GatewayDirectoryEntryDTO(
            name=entry.name,
            path=str(Path(entry.path).resolve()),
        )
        for entry in directories[:limit]
    ]
    return entries, len(directories) > limit


async def _directory_listing(
    raw_path: str | None,
    *,
    limit: int,
) -> GatewayDirectoryListDTO:
    root_path = _resolve_local_directory(raw_path)
    entries, truncated = await asyncio.to_thread(
        _scan_local_directories,
        root_path,
        limit,
    )
    parent_path = root_path.parent if root_path.parent != root_path else None
    return GatewayDirectoryListDTO(
        path=str(root_path),
        parent_path=str(parent_path) if parent_path is not None else None,
        home_path=str(Path.home().resolve()),
        entries=entries,
        truncated=truncated,
        limit=limit,
    )


async def _managed_workspace_list(
    registry: GatewayWorkspaceRegistry,
) -> GatewayManagedWorkspaceListDTO:
    return GatewayManagedWorkspaceListDTO(
        gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
        gateway_name="本机 Gateway",
        connection_kind="local",
        items=await list_direct_managed_workspaces(registry),
    )


def get_registry(request: Request) -> GatewayWorkspaceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return registry


def get_gateway_config_reload_service(
    request: Request,
) -> GatewayConfigReloadService:
    service = getattr(request.app.state, "gateway_config_reload", None)
    if not isinstance(service, GatewayConfigReloadService):
        raise RuntimeError("Gateway 配置重载服务尚未初始化")
    return service


def get_user_access_service(request: Request) -> UserAccessService:
    service = getattr(request.app.state, "user_access_service", None)
    if not isinstance(service, UserAccessService):
        raise RuntimeError("Gateway 用户访问服务尚未初始化")
    return service


def get_user_profile_store(request: Request) -> UserProfileStore:
    store = getattr(request.app.state, "user_profile_store", None)
    if not isinstance(store, UserProfileStore):
        raise RuntimeError("Gateway 用户 profile 存储尚未初始化")
    return store


def _current_user_access(
    request: Request,
    service: UserAccessService,
) -> UserAccessContext:
    context = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if context is None:
        raise HTTPException(status_code=401, detail="user_session_required")
    return context
