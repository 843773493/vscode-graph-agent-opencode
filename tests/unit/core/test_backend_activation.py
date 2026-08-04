import os
from pathlib import Path

from app.core import path_utils


def test_initialize_directories_only_creates_current_workspace_root(tmp_path, monkeypatch):
    user_workspace_root = tmp_path / "default-workspace"
    runtime_workspace_root = tmp_path / "remote-workspace"
    monkeypatch.setenv("BOXTEAM_USER_WORKSPACE_ROOT", str(user_workspace_root))
    monkeypatch.setenv("WORKSPACE_ROOT", str(runtime_workspace_root))

    path_utils.initialize_directories()

    assert (runtime_workspace_root / ".boxteam" / "sessions").is_dir()
    assert not user_workspace_root.exists()
