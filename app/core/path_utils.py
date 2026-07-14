from __future__ import annotations
import os
from pathlib import Path
from typing import Optional
from app.core.exceptions import ForbiddenError


def get_user_workspace_root() -> Path:
    """获取用户级持久工作区根目录。优先使用显式配置，未配置时回退到用户主目录下的隐藏目录。"""
    configured_root = os.environ.get("BOXTEAM_USER_WORKSPACE_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()

    home_dir = Path.home().resolve()
    return home_dir / ".boxteams" / "boxteam_workspace"


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

def get_checkpoints_dir() -> Path:
    return get_boxteam_root() / "checkpoints"


def get_session_changes_dir(session_id: str) -> Path:
    """获取某个会话的可读文件变更记录目录。"""
    return get_session_path(session_id) / "changes"


def initialize_directories() -> None:
    """初始化所有必需的目录，应该在应用启动时显式调用"""
    get_boxteam_root().mkdir(exist_ok=True, parents=True)
    get_sessions_dir().mkdir(exist_ok=True, parents=True)
    get_checkpoints_dir().mkdir(exist_ok=True, parents=True)
    get_logs_dir().mkdir(exist_ok=True, parents=True)
    get_artifacts_dir().mkdir(exist_ok=True, parents=True)
    get_cache_dir().mkdir(exist_ok=True, parents=True)
    get_user_workspace_root().mkdir(exist_ok=True, parents=True)


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
