import os
from pathlib import Path

from app.core import path_utils


def test_initialize_directories_creates_user_workspace_root(tmp_path, monkeypatch):
    user_workspace_root = tmp_path / ".BoxTeamWorkspace"
    monkeypatch.setenv("BOXTEAM_USER_WORKSPACE_ROOT", str(user_workspace_root))
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)

    path_utils.initialize_directories()

    assert user_workspace_root.exists()
    assert user_workspace_root.is_dir()
