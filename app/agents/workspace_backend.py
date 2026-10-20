from __future__ import annotations

from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend

BOXTEAM_ARTIFACTS_ROOT = "/.boxteam"


def build_workspace_backend(workspace_root: Path) -> CompositeBackend:
    """仅保留框架内部虚拟工作区；会话产物通过原 owner 的精确 URI 读取。"""
    return CompositeBackend(
        default=FilesystemBackend(root_dir=str(workspace_root), virtual_mode=True),
        routes={}, artifacts_root=BOXTEAM_ARTIFACTS_ROOT,
    )
