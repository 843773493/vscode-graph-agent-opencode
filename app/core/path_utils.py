from __future__ import annotations
import os
from pathlib import Path
from typing import Optional
from app.core.exceptions import ForbiddenError


# Workspace root configuration - use environment variable if set, default to ./workspace
if "WORKSPACE_ROOT" in os.environ:
    WORKSPACE_ROOT = Path(os.environ["WORKSPACE_ROOT"]).resolve()
else:
    WORKSPACE_ROOT = Path(os.getcwd()) / "workspace"

BOXTEAM_ROOT = WORKSPACE_ROOT / ".boxteam"
BOXTEAM_ROOT.mkdir(exist_ok=True, parents=True)

# BoxTeam internal directories
SESSIONS_DIR = BOXTEAM_ROOT / "sessions"
LOGS_DIR = BOXTEAM_ROOT / "logs"
ARTIFACTS_DIR = BOXTEAM_ROOT / "artifacts"
CACHE_DIR = BOXTEAM_ROOT / "cache"

# Create all required directories
SESSIONS_DIR.mkdir(exist_ok=True, parents=True)
LOGS_DIR.mkdir(exist_ok=True, parents=True)
ARTIFACTS_DIR.mkdir(exist_ok=True, parents=True)
CACHE_DIR.mkdir(exist_ok=True, parents=True)


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
    
    # Ensure the resulting path is still within the base directory
    if not str(joined).startswith(str(base) + os.sep) and joined != base:
        raise ForbiddenError("Path traversal detected")
    
    return joined


def get_session_path(session_id: str) -> Path:
    """Get the directory path for a specific session"""
    return safe_join(SESSIONS_DIR, session_id)


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
    return safe_join(WORKSPACE_ROOT, path)
