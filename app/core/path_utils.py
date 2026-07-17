from __future__ import annotations

import os
from pathlib import Path

from app.core.exceptions import ForbiddenError
from app.core.storage_migration import migrate_workspace_storage_layout


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


def get_user_config_path() -> Path:
    """获取用户级全局配置文件。"""
    configured_path = os.environ.get("BOXTEAM_USER_CONFIG_PATH")
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return get_user_config_root() / "boxteam.jsonc"


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
    """获取工作区根目录。优先从 WORKSPACE_ROOT 环境变量读取，未设置时回退到用户级持久工作区根目录。"""
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

def get_logs_dir() -> Path:
    return get_boxteam_root() / "logs"

def get_artifacts_dir() -> Path:
    return get_boxteam_root() / "artifacts"

def get_cache_dir() -> Path:
    return get_boxteam_root() / "cache"

def get_session_changes_dir(session_id: str) -> Path:
    """获取某个会话的可读文件变更记录目录。"""
    return get_session_path(session_id) / "changes"


def get_session_logs_dir(session_id: str) -> Path:
    return get_session_path(session_id) / "logs"


def initialize_directories() -> None:
    """初始化所有必需的目录，应该在应用启动时显式调用"""
    get_boxteam_root().mkdir(exist_ok=True, parents=True)
    get_sessions_dir().mkdir(exist_ok=True, parents=True)
    get_logs_dir().mkdir(exist_ok=True, parents=True)
    get_artifacts_dir().mkdir(exist_ok=True, parents=True)
    get_cache_dir().mkdir(exist_ok=True, parents=True)
    migrate_workspace_storage_layout(
        boxteam_root=get_boxteam_root(),
        sessions_root=get_sessions_dir(),
    )


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
    """Get the directory path for a specific session"""
    return safe_join(get_sessions_dir(), session_id)


def get_session_file(session_id: str) -> Path:
    """Get the JSON metadata file path for a session"""
    return get_session_path(session_id) / "session.json"


def ensure_session_dir(session_id: str) -> Path:
    """Ensure session directory exists, create if not"""
    session_dir = get_session_path(session_id)
    session_dir.mkdir(exist_ok=True, parents=True)
    return session_dir


def validate_workspace_path(path: str) -> Path:
    """Validate a path is within the workspace root"""
    return safe_join(get_workspace_root(), path)
