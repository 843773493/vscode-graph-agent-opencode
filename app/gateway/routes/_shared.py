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

from app.core.exceptions import ForbiddenError
from app.core.path_utils import get_gateway_root, resolve_native_file_path, safe_join
from app.core.resource_uri import parse_resource_uri, resource_display_uri
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
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.schemas.gateway.directory import (
    GatewayDirectoryEntryDTO,
    GatewayDirectoryListDTO,
)
from app.schemas.gateway.workspaces import GatewayManagedWorkspaceListDTO

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


def resolve_management_directory(
    request: Request, value: str, *, base_path: str | None = None,
) -> Path:
    """目录选择与注册共用目标 Gateway 的根绑定；相对输入必须有显式基准。"""
    if "://" in value:
        parsed = parse_resource_uri(value, local_gateway_id=request.app.state.gateway_id)
        if (parsed.scope != "gateway" or parsed.kind != "files"
                or parsed.scope_id != request.app.state.gateway_id
                or parsed.gateway_authority is not None):
            raise ValueError("目录 URI 不属于选定的 Gateway 原生目录")
        try:
            root = request.app.state.gateway_state.resolve_native_directory(
                gateway_id=parsed.scope_id, directory_id=parsed.tail_segments[0],
            )
        except KeyError as error:
            raise HTTPException(404, "所选目录绑定不存在") from error
        try:
            return safe_join(Path(root), *parsed.tail_segments[1:])
        except ForbiddenError as error:
            raise HTTPException(403, "目录 URI 超出选定根的访问能力") from error
    native = Path(value).expanduser()
    if not native.is_absolute() and base_path is None:
        raise ValueError("原生目录必须是绝对路径；相对路径需要选定目录 URI")
    base = resolve_management_directory(request, base_path) if base_path else Path.home()
    try:
        return resolve_native_file_path(base, value, allow_absolute_outside=True)
    except ForbiddenError as error:
        raise HTTPException(403, "相对路径超出选定目录的访问能力") from error


def _management_directory_uri(request: Request, root: Path) -> str:
    gateway_id = request.app.state.gateway_id
    directory_id = request.app.state.gateway_state.bind_native_directory(
        gateway_id=gateway_id, native_root=str(root),
    )
    return resource_display_uri(
        scope="gateway", scope_id=gateway_id, kind="files", tail_segments=(directory_id,),
    )


def _scan_local_directories(
    root_path: Path,
    limit: int,
) -> tuple[list[Path], bool]:
    with os.scandir(root_path) as directory_iterator:
        directories = [
            entry for entry in directory_iterator if entry.is_dir(follow_symlinks=False)
        ]
    directories.sort(key=lambda entry: (entry.name.lower(), entry.name))
    entries = [Path(entry.path).resolve() for entry in directories[:limit]]
    return entries, len(directories) > limit


async def _directory_listing(
    request: Request,
    raw_path: str | None,
    *,
    limit: int,
    base_path: str | None = None,
) -> GatewayDirectoryListDTO:
    def collect() -> GatewayDirectoryListDTO:
        root = resolve_management_directory(request, raw_path or str(Path.home()), base_path=base_path)
        if not root.exists():
            raise HTTPException(400, "所选目录不存在")
        if not root.is_dir():
            raise HTTPException(400, "所选资源不是目录")
        try:
            entries, truncated = _scan_local_directories(root, limit)
        except OSError as error:
            # 原异常留在本机原因链；普通响应发布阶段与系统错误码，不复制根路径。
            raise HTTPException(403 if isinstance(error, PermissionError) else 400,
                                f"目录枚举失败: {type(error).__name__}, errno={error.errno}") from error
        return GatewayDirectoryListDTO(
            name=root.name or "文件系统根目录",
            path=_management_directory_uri(request, root),
            parent_path=_management_directory_uri(request, root.parent) if root.parent != root else None,
            home_path=_management_directory_uri(request, Path.home().resolve()),
            entries=[GatewayDirectoryEntryDTO(name=entry.name, path=_management_directory_uri(request, entry))
                     for entry in entries],
            truncated=truncated, limit=limit,
        )

    return await asyncio.to_thread(collect)


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


def get_workspace_runtime_controller(
    request: Request,
) -> GatewayWorkspaceRuntimeController:
    controller = getattr(request.app.state, "workspace_runtime_controller", None)
    if not isinstance(controller, GatewayWorkspaceRuntimeController):
        raise TypeError("Gateway 工作区运行时控制器尚未初始化")
    return controller
