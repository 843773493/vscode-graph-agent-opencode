from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from app.core.exceptions import ForbiddenError
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_creation import SessionCreationService
from app.core.session_subtree_delete import SessionSubtreeDeleteService
from app.core.workspace_identity import load_or_create_workspace_id


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
    """获取工作区根目录。

    优先从 WORKSPACE_ROOT 环境变量读取，未设置时回退到用户级持久工作区根目录。
    """
    workspace_root = os.environ.get("WORKSPACE_ROOT")
    if not workspace_root:
        return get_user_workspace_root()

    return Path(workspace_root).resolve()


def get_runtime_workspace_root() -> Path:
    """获取当前后端进程应使用的工作区根目录。"""
    workspace_root = os.environ.get("WORKSPACE_ROOT")
    if workspace_root:
        return Path(workspace_root).resolve()

    return get_user_workspace_root()


def get_boxteam_root() -> Path:
    return get_workspace_root() / ".boxteam"


def get_sessions_dir() -> Path:
    return get_boxteam_root() / "sessions"


def _build_session_catalog_components(
    sessions_root: Path,
) -> tuple[SessionCatalogPathResolver, SessionCreationService]:
    """构造 SQLite catalog 的 resolver 与原子创建服务（共享同一 store）。

    探测逻辑（同一工作区单数据源，不做双读）：

    - ``navigation/session-catalog.sqlite`` 存在 → 构造 store（打开时
      fail-closed 校验 ``user_version``）→ creation/delete service → 新
      resolver；
    - SQLite catalog 不存在且旧 ``navigation/session-catalog-index.json``
      存在 → ``RuntimeError``（提示先经 ``SessionCatalogMigrator`` 维护
      操作完成一次性迁移，拒绝双读旧 JSON）；
    - 两者都不存在（全新工作区）→ store 初始化建空 catalog → services →
      新 resolver。

    workspace_id 取自 ``app/core/workspace_identity.py`` 的
    ``load_or_create_workspace_id``（与 container 装配使用同一份
    ``.boxteam/workspace-identity.json``，保证同一工作区后端 UUID 一致）。
    store 的 SQLite 连接随 lru_cache 的 resolver 实例常开，进程生命周期
    内复用，不提供单独关闭入口。
    """
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
            "拒绝双读旧 JSON；请先通过 SessionCatalogMigrator 维护"
            f"操作完成一次性迁移: legacy_index={legacy_index_path}, "
            f"catalog={database_path}"
        )
    # catalog 已存在时打开并 fail-closed 校验 user_version；全新工作区则
    # 建表初始化空 catalog（store 构造即完成建表与版本检查）。
    store = SessionCatalogStore(database_path, sessions_root)
    workspace_id = load_or_create_workspace_id(workspace_root)
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
    return resolver, creation_service


@lru_cache(maxsize=32)
def _cached_session_catalog_components(
    sessions_root: str,
) -> tuple[SessionCatalogPathResolver, SessionCreationService]:
    """按会话根目录缓存共享的 catalog resolver 与创建服务。"""
    return _build_session_catalog_components(Path(sessions_root))


def get_session_path_resolver(
    sessions_root: Path | None = None,
) -> SessionCatalogPathResolver:
    """获取会话路径解析器；SQLite catalog 是唯一权威数据源。"""
    resolved_root = (sessions_root or get_sessions_dir()).resolve()
    return _cached_session_catalog_components(str(resolved_root))[0]


def get_session_creation_service(
    sessions_root: Path | None = None,
) -> SessionCreationService:
    """获取与路径解析器共享 store 的原子 Session 创建服务。"""
    resolved_root = (sessions_root or get_sessions_dir()).resolve()
    return _cached_session_catalog_components(str(resolved_root))[1]


def get_logs_dir() -> Path:
    return get_boxteam_root() / "logs"


def get_artifacts_dir() -> Path:
    return get_boxteam_root() / "artifacts"


def get_cache_dir() -> Path:
    return get_boxteam_root() / "cache"


def get_session_changes_dir(session_id: str) -> Path:
    """获取某个会话的可读文件变更记录目录。"""
    return get_session_path(session_id) / "changes"


def initialize_directories() -> None:
    """初始化当前工作区运行时所需的目录与 SQLite catalog。"""
    get_boxteam_root().mkdir(exist_ok=True, parents=True)
    get_sessions_dir().mkdir(exist_ok=True, parents=True)
    get_logs_dir().mkdir(exist_ok=True, parents=True)
    get_artifacts_dir().mkdir(exist_ok=True, parents=True)
    get_cache_dir().mkdir(exist_ok=True, parents=True)
    get_session_path_resolver().initialize()


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
    if not str(joined).startswith(str(base) + os.sep) and joined != base:
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
