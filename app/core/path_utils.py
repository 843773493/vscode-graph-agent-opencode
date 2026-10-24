from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol

from app.core.exceptions import ForbiddenError
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_creation import SessionCreationService
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from app.core.workspace_identity import (
    load_or_create_workspace_id,
    validate_workspace_id,
)

_WORKSPACE_CONTEXT: ContextVar[tuple[str, Path] | None] = ContextVar(
    "boxteam_workspace_context",
    default=None,
)


def is_resource_uri_input(value: str) -> bool:
    """只识别输入开头的 URI 协议；原生绝对路径优先按目标平台解释。"""
    return not Path(value).is_absolute() and re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", value) is not None


@dataclass(slots=True)
class SessionCatalogComponents:
    """由显式生命周期 owner 持有的同一组 catalog 组件。"""

    sessions_root: Path
    resolver: SessionCatalogPathResolver
    creation_service: SessionCreationService
    _closed: bool = False

    def get_catalog_components(
        self, sessions_root: Path, workspace_id: str
    ) -> SessionCatalogComponents:
        if self._closed:
            raise RuntimeError("Session catalog owner 已关闭，禁止重新打开")
        if (
            sessions_root != self.sessions_root
            or workspace_id != self.resolver.workspace_id
        ):
            raise RuntimeError(
                "Session catalog 与请求工作区不一致: "
                f"requested={workspace_id}:{sessions_root}, "
                f"owned={self.resolver.workspace_id}:{self.sessions_root}"
            )
        return self

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.resolver.catalog_store.close()


class SessionCatalogProvider(Protocol):
    def get_catalog_components(
        self, sessions_root: Path, workspace_id: str
    ) -> SessionCatalogComponents: ...


_CATALOG_PROVIDER: ContextVar[SessionCatalogProvider | None] = ContextVar(
    "boxteam_session_catalog_provider", default=None
)


@contextmanager
def catalog_provider_scope(provider: SessionCatalogProvider) -> Iterator[None]:
    """只委派当前显式 owner，不创建或缓存第二套组件。"""
    token = _CATALOG_PROVIDER.set(provider)
    try:
        yield
    finally:
        _CATALOG_PROVIDER.reset(token)


@contextmanager
def workspace_context(
    workspace_id: str,
    workspace_root: Path,
    *,
    session_catalog: SessionCatalogComponents | None = None,
) -> Iterator[None]:
    """在当前请求/任务上下文中绑定已解析的工作区身份与根目录。"""

    binding = (
        validate_workspace_id(workspace_id),
        workspace_root.expanduser().resolve(strict=True),
    )
    token: Token[tuple[str, Path] | None] = _WORKSPACE_CONTEXT.set(binding)
    try:
        if session_catalog is None:
            yield
        else:
            session_catalog.get_catalog_components(
                binding[1] / ".boxteam" / "sessions", binding[0]
            )
            with catalog_provider_scope(session_catalog):
                yield
    finally:
        _WORKSPACE_CONTEXT.reset(token)


def resolve_boxteam_home(home: Path | None = None) -> Path:
    """按显式环境变量或指定用户目录解析 BoxTeam 全局根目录。"""
    configured_root = os.environ.get("BOXTEAM_HOME")
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return (home or Path.home()).expanduser().resolve() / ".boxteams"


def get_boxteam_home() -> Path:
    """获取 BoxTeam 的用户级安装与全局数据根目录。"""
    return resolve_boxteam_home()


def get_user_config_root() -> Path:
    """获取用户级全局配置目录。"""
    return get_boxteam_home() / "config"


def get_user_gateway_config_path() -> Path:
    """获取 Gateway 用户级控制面配置文件。"""
    return get_user_config_root() / "gateway.jsonc"


def get_user_gateway_local_config_path() -> Path:
    """获取 Gateway 用户级本地覆盖配置文件。"""
    return get_user_config_root() / "gateway_local.jsonc"


def get_user_workspace_config_path() -> Path:
    """获取 Workspace Backend 用户级业务配置文件。"""
    return get_user_config_root() / "workspace.jsonc"


def get_user_workspace_local_config_path() -> Path:
    """获取 Workspace 用户级本地覆盖配置文件。"""
    return get_user_config_root() / "workspace_local.jsonc"


def get_user_gateway_schema_path() -> Path:
    """获取安装后的 Gateway 配置 schema。"""
    return get_user_config_root() / "gateway_schema.jsonc"


def get_user_workspace_schema_path() -> Path:
    """获取安装后的 Workspace 配置 schema。"""
    return get_user_config_root() / "workspace_schema.jsonc"


def get_workspace_config_path(workspace_root: Path) -> Path:
    """获取显式工作区的业务配置覆盖文件。"""
    return workspace_root.expanduser().resolve() / ".boxteam" / "workspace.jsonc"


def get_user_env_path() -> Path:
    """获取用户级统一环境配置文件。"""
    return get_user_config_root() / ".env"


def get_user_workspace_root() -> Path:
    """获取用户级持久工作区根目录。优先使用显式配置，未配置时回退到用户主目录下的隐藏目录。"""
    configured_root = os.environ.get("BOXTEAM_USER_WORKSPACE_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    return get_boxteam_home() / "boxteam_workspace"


def get_gateway_root() -> Path:
    """获取跨工作区 Gateway 控制面数据目录。"""
    configured_root = os.environ.get("BOXTEAM_GATEWAY_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return get_boxteam_home() / "state" / "gateway"


def get_workspace_root() -> Path:
    """取得显式寻址请求绑定的工作区根目录，不读取进程环境默认值。"""

    binding = _WORKSPACE_CONTEXT.get()
    if binding is None:
        raise RuntimeError("工作区根目录解析必须位于显式 workspace_id 请求上下文中")
    return binding[1]


def get_workspace_id() -> str:
    """取得显式寻址请求绑定的 workspace UUID。"""

    binding = _WORKSPACE_CONTEXT.get()
    if binding is None:
        raise RuntimeError("workspace_id 解析必须位于显式工作区请求上下文中")
    return binding[0]


def get_boxteam_root() -> Path:
    return get_workspace_root() / ".boxteam"


def get_sessions_dir() -> Path:
    return get_boxteam_root() / "sessions"


def build_session_catalog_components(
    sessions_root: Path,
    workspace_id: str,
) -> SessionCatalogComponents:
    """构造 SQLite catalog 的 resolver 与原子创建服务（共享同一 store）。

    探测逻辑（同一工作区单数据源，不做双读）：

    - ``navigation/session-catalog.sqlite`` 存在 → 构造 store（打开时
      fail-closed 校验 ``user_version``）→ creation/delete service → 新
      resolver；
    - SQLite catalog 不存在且旧 ``navigation/session-catalog-index.json``
      存在 → ``RuntimeError``（拒绝忽略旧权威索引并初始化空 catalog；此版本
      不提供旧 Session Catalog 迁移，也不双读旧 JSON）；
    - 两者都不存在（全新工作区）→ store 初始化建空 catalog → services →
      新 resolver。

    workspace_id 取自 ``app/core/workspace_identity.py`` 的
    ``load_or_create_workspace_id``（与 container 装配使用同一份
    ``.boxteam/workspace-identity.json``，保证同一工作区后端 UUID 一致）。
    返回的组件必须由显式 owner 持有并关闭；路径 helper 只委派 owner。
    """
    sessions_root = sessions_root.expanduser().resolve()
    if sessions_root.name == "sessions":
        navigation_root = sessions_root.parent / "navigation"
        if sessions_root.parent.name == ".boxteam":
            workspace_root = sessions_root.parent.parent
        else:
            # 非标准布局（如测试把 sessions 根直接放在临时目录下）：
            # 会话数据根的父目录即视作工作区根。
            workspace_root = sessions_root.parent
    else:
        # TODO: 测试与嵌入式调用仍允许传入任意 sessions 根目录；统一工作区
        # 根目录约定后删除该分支。
        navigation_root = (
            sessions_root.parent / f".{sessions_root.name}-session-navigation"
        )
        workspace_root = sessions_root.parent

    database_path = navigation_root / "session-catalog.sqlite"
    legacy_index_path = navigation_root / "session-catalog-index.json"
    if not database_path.is_file() and legacy_index_path.is_file():
        raise RuntimeError(
            "检测到旧形态会话目录权威索引但 SQLite session catalog 缺失，"
            "拒绝忽略旧数据并初始化空 catalog；此版本不提供旧 Session Catalog "
            "迁移，请从当前格式工作区恢复或人工处理旧数据: "
            f"legacy_index={legacy_index_path}, "
            f"catalog={database_path}"
        )
    # catalog 已存在时打开并 fail-closed 校验 user_version；全新工作区则
    # 建表初始化空 catalog（store 构造即完成建表与版本检查）。
    workspace_id = validate_workspace_id(workspace_id)
    actual_workspace_id = load_or_create_workspace_id(workspace_root)
    if actual_workspace_id != workspace_id:
        raise RuntimeError(
            "会话 catalog workspace UUID 与工作区身份文件不一致: "
            f"expected={workspace_id}, actual={actual_workspace_id}"
        )
    store = SessionCatalogStore(database_path, sessions_root)
    try:
        creation_service = SessionCreationService(
            store=store,
            sessions_root=sessions_root,
            workspace_id=workspace_id,
        )
        delete_service = SessionSubtreeDeleteService(
            store=store,
            sessions_root=sessions_root,
            workspace_id=workspace_id,
        )
        resolver = SessionCatalogPathResolver(
            store=store,
            sessions_root=sessions_root,
            workspace_id=workspace_id,
            delete_service=delete_service,
        )
        return SessionCatalogComponents(sessions_root, resolver, creation_service)
    except BaseException as error:
        try:
            store.close()
        except Exception as close_error:  # noqa: BLE001 - 关闭失败须与原始失败一起暴露。
            raise BaseExceptionGroup(
                "Session catalog 构造失败且关闭失败", [error, close_error]
            ) from None
        raise


def _resolve_session_scope(
    sessions_root: Path | None,
    workspace_id: str | None,
) -> tuple[Path, str]:
    if sessions_root is None:
        binding = _WORKSPACE_CONTEXT.get()
        if binding is None:
            raise RuntimeError(
                "获取 Session catalog 必须传入 sessions_root 或位于显式工作区请求上下文中"
            )
        root = binding[1] / ".boxteam" / "sessions"
        resolved_workspace_id = binding[0]
    else:
        root = sessions_root.expanduser().resolve()
        if root.name == "sessions" and root.parent.name == ".boxteam":
            workspace_root = root.parent.parent
        else:
            # TODO: 测试与嵌入式调用仍允许传入任意 sessions 根目录；统一工作区
            # 根目录约定后删除该分支。
            workspace_root = root.parent
        resolved_workspace_id = workspace_id or load_or_create_workspace_id(
            workspace_root
        )
    if workspace_id is not None:
        explicit_id = validate_workspace_id(workspace_id)
        if explicit_id != resolved_workspace_id:
            raise RuntimeError(
                "Session catalog 显式 workspace UUID 与上下文身份不一致: "
                f"explicit={explicit_id}, context={resolved_workspace_id}"
            )
    return root.resolve(), validate_workspace_id(resolved_workspace_id)



def get_session_catalog_components(
    sessions_root: Path | None = None,
    *,
    workspace_id: str | None = None,
) -> SessionCatalogComponents:
    resolved_root, resolved_workspace_id = _resolve_session_scope(
        sessions_root, workspace_id
    )
    provider = _CATALOG_PROVIDER.get()
    if provider is None:
        raise RuntimeError(
            "获取 Session catalog 必须绑定显式生命周期 owner: "
            f"workspace_id={resolved_workspace_id}, sessions_root={resolved_root}"
        )
    return provider.get_catalog_components(resolved_root, resolved_workspace_id)


def get_session_path_resolver(
    sessions_root: Path | None = None,
    *,
    workspace_id: str | None = None,
) -> SessionCatalogPathResolver:
    """从当前 owner 取得唯一 SQLite catalog 路径解析器。"""
    return get_session_catalog_components(
        sessions_root, workspace_id=workspace_id
    ).resolver


def get_session_creation_service(
    sessions_root: Path | None = None,
    *,
    workspace_id: str | None = None,
) -> SessionCreationService:
    """从当前 owner 取得与 resolver 共用 store 的原子创建服务。"""
    return get_session_catalog_components(
        sessions_root, workspace_id=workspace_id
    ).creation_service


def get_logs_dir() -> Path:
    return get_boxteam_root() / "logs"


def get_artifacts_dir() -> Path:
    return get_boxteam_root() / "artifacts"


def get_cache_dir() -> Path:
    return get_boxteam_root() / "cache"


def get_session_changes_dir(session_id: str) -> Path:
    """获取某个会话的可读文件变更记录目录。"""
    return get_session_path(session_id) / "changes"


def initialize_directories(workspace_id: str, workspace_root: Path) -> None:
    """初始化 mount 集合中一个显式工作区的业务数据目录与 catalog。"""
    resolved_root = workspace_root.expanduser().resolve(strict=True)
    boxteam_root = resolved_root / ".boxteam"
    (boxteam_root / "sessions").mkdir(exist_ok=True, parents=True)
    for name in ("logs", "artifacts", "cache"):
        (boxteam_root / name).mkdir(exist_ok=True, parents=True)
    get_session_path_resolver(
        boxteam_root / "sessions",
        workspace_id=workspace_id,
    ).initialize()


def resolve_native_file_path(
    base_path: Path, value: str, *, allow_absolute_outside: bool = False,
) -> Path:
    """只在目标机器解释原生路径；相对输入始终锚定显式目录。"""
    if "\x00" in value:
        raise ValueError("文件路径不能包含 NUL 字符")
    if os.name != "nt" and PureWindowsPath(value).drive:
        raise ValueError("Windows 盘符或 UNC 路径必须交给 Windows 目标解释")
    resolved_base = base_path.resolve()
    requested_path = Path(value).expanduser()
    if requested_path.drive and not requested_path.is_absolute():
        raise ValueError("Windows 盘符路径必须提供绝对根，不能依赖目标进程的盘符当前目录")
    resolved_path = (
        requested_path
        if requested_path.is_absolute()
        else resolved_base / requested_path
    ).resolve()
    if not resolved_path.is_relative_to(resolved_base) and not (
        requested_path.is_absolute() and allow_absolute_outside
    ):
        raise ForbiddenError("文件路径超出当前目标目录的访问能力")
    return resolved_path


def safe_join(base_path: Path, *paths: str) -> Path:
    """
    Safely join paths and prevent directory traversal attacks.

    Args:
        base_path: Base directory to restrict access to
        *paths: Path components to join

    Returns:
        Resolved absolute path

    Raises:
        ForbiddenError: If path traversal is detected
    """
    base = base_path.resolve()
    joined = base.joinpath(*paths).resolve()

    # 确保生成的路径仍然在基础目录范围内
    if not joined.is_relative_to(base):
        raise ForbiddenError("Path traversal detected")

    return joined


def get_session_path(session_id: str) -> Path:
    """通过稳定 ID 解析会话当前所在的物理目录。"""
    try:
        return get_session_path_resolver().resolve_session_node(session_id)
    except KeyError as error:
        raise FileNotFoundError(f"会话物理目录不存在: {session_id}") from error


def get_session_file(session_id: str) -> Path:
    """Get the JSON metadata file path for a session"""
    return get_session_path(session_id) / "session.json"


def validate_workspace_path(path: str) -> Path:
    """Validate a path is within the workspace root"""
    return safe_join(get_workspace_root(), path)
