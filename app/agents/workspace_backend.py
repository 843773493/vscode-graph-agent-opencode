from __future__ import annotations

from pathlib import Path

from deepagents.backends import CompositeBackend, LocalShellBackend

BOXTEAM_ARTIFACTS_ROOT = "/.boxteam"


def build_workspace_backend(workspace_root: Path) -> CompositeBackend:
    """构建统一的 DeepAgents 工作区后端，并隔离框架运行产物。"""
    workspace_files = LocalShellBackend(
        root_dir=str(workspace_root),
        virtual_mode=True,
    )
    return CompositeBackend(
        default=workspace_files,
        routes={},
        artifacts_root=BOXTEAM_ARTIFACTS_ROOT,
    )
