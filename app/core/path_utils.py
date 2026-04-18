from __future__ import annotations
import os
from pathlib import Path
from typing import Optional
from app.core.exceptions import ForbiddenError


# 工作区根目录配置 - 如果设置了环境变量则使用，否则默认为 ./workspace
def get_workspace_root() -> Path:
    """获取工作区根目录，延迟初始化避免导入时提前创建目录"""
    if "WORKSPACE_ROOT" in os.environ:
        return Path(os.environ["WORKSPACE_ROOT"]).resolve()
    else:
        return Path(os.getcwd()) / "workspace"

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

def initialize_directories() -> None:
    """初始化所有必需的目录，应该在应用启动时显式调用"""
    get_boxteam_root().mkdir(exist_ok=True, parents=True)
    get_sessions_dir().mkdir(exist_ok=True, parents=True)
    get_logs_dir().mkdir(exist_ok=True, parents=True)
    get_artifacts_dir().mkdir(exist_ok=True, parents=True)
    get_cache_dir().mkdir(exist_ok=True, parents=True)


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
