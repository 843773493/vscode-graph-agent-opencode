from __future__ import annotations

from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend

from app.core.path_utils import get_session_path_resolver, safe_join
from app.core.session_paths import SessionPathResolver

BOXTEAM_ARTIFACTS_ROOT = "/.boxteam"
SESSION_ARTIFACT_ROUTE = "/session-artifacts/"


class SessionArtifactBackend(FilesystemBackend):
    """把稳定 session ID 虚拟路径映射到可移动的物理会话目录。"""

    def __init__(self, workspace_root: Path) -> None:
        sessions_root = workspace_root / ".boxteam" / "sessions"
        super().__init__(root_dir=sessions_root, virtual_mode=True)
        self._path_resolver: SessionPathResolver = get_session_path_resolver(
            sessions_root
        )

    def _resolve_path(self, key: str) -> Path:
        virtual_path = Path(key.lstrip("/"))
        if not virtual_path.parts:
            return self.cwd
        if ".." in virtual_path.parts:
            raise ValueError(f"会话产物虚拟路径无效: {key}")
        session_id = virtual_path.parts[0]
        try:
            session_root = self._path_resolver.resolve_session_node(session_id)
        except KeyError as error:
            raise FileNotFoundError(
                f"会话产物路径引用了不存在的稳定 ID: {session_id}"
            ) from error
        return safe_join(session_root, *virtual_path.parts[1:])

    def _to_virtual_path(self, path: Path) -> str:
        resolved = path.resolve()
        session_nodes = sorted(
            (
                node
                for node in self._path_resolver.list_nodes()
                if node.kind == "session"
            ),
            key=lambda node: len(node.path.parts),
            reverse=True,
        )
        for node in session_nodes:
            try:
                tail = resolved.relative_to(node.path)
            except ValueError:
                continue
            return f"/{node.node_id}/{tail.as_posix()}"
        return super()._to_virtual_path(path)


def build_workspace_backend(workspace_root: Path) -> CompositeBackend:
    """构建统一的 DeepAgents 工作区后端，并隔离框架运行产物。"""
    workspace_files = FilesystemBackend(
        root_dir=str(workspace_root),
        virtual_mode=True,
    )
    session_artifacts = SessionArtifactBackend(workspace_root)
    return CompositeBackend(
        default=workspace_files,
        routes={
            SESSION_ARTIFACT_ROUTE: session_artifacts,
        },
        artifacts_root=BOXTEAM_ARTIFACTS_ROOT,
    )
