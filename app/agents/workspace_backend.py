from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import FileData, ReadResult
from deepagents.backends.utils import slice_read_response

from app.core.path_utils import get_session_path_resolver, safe_join
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.services.infrastructure.attachment_blob_catalog.store import (
    StoredAttachmentContent,
)

BOXTEAM_ARTIFACTS_ROOT = "/.boxteam"
SESSION_ARTIFACT_ROUTE = "/session-artifacts/"


class SessionArtifactBackend(FilesystemBackend):
    """把稳定 session ID 虚拟路径映射到可移动的物理会话目录。"""

    def __init__(
        self, workspace_root: Path, *,
        attachment_reader: Callable[[str, str], StoredAttachmentContent] | None = None,
    ) -> None:
        sessions_root = workspace_root / ".boxteam" / "sessions"
        super().__init__(root_dir=sessions_root, virtual_mode=True)
        self._path_resolver: SessionCatalogPathResolver = get_session_path_resolver(
            sessions_root
        )
        self._attachment_reader = attachment_reader

    def read_attachment(self, file_path: str) -> StoredAttachmentContent:
        """逻辑附件只交给运行时注入的 capability，永不解析旧物理目录。"""
        parts = Path(file_path.lstrip("/")).parts
        if len(parts) != 3 or parts[1] != "attachments" or ".." in parts:
            raise ValueError(f"附件工具必须使用精确 logical reference: {file_path}")
        if self._attachment_reader is None:
            raise PermissionError("附件读取缺少当前 SessionThread 工具 capability")
        session_id, _, attachment_id = parts
        return self._attachment_reader(session_id, f"boxteam-session://{session_id}/attachments/{attachment_id}")

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        parts = Path(file_path.lstrip("/")).parts
        if len(parts) < 2 or parts[1] != "attachments":
            return super().read(file_path, offset=offset, limit=limit)
        try:
            content = self.read_attachment(file_path)
            if content.content_type.startswith("text/"):
                file_data = FileData(content=content.data.decode("utf-8"), encoding="utf-8")
                sliced = slice_read_response(file_data, offset, limit)
                return sliced if isinstance(sliced, ReadResult) else ReadResult(file_data=FileData(content=sliced, encoding="utf-8"))
            return ReadResult(file_data=FileData(content=base64.b64encode(content.data).decode("ascii"), encoding="base64"))
        except (OSError, RuntimeError, ValueError) as error:
            return ReadResult(error=f"附件读取失败: {error}")

    def _resolve_path(self, key: str) -> Path:
        virtual_path = Path(key.lstrip("/"))
        if not virtual_path.parts:
            return self.cwd
        if ".." in virtual_path.parts:
            raise ValueError(f"会话产物虚拟路径无效: {key}")
        if len(virtual_path.parts) > 1 and virtual_path.parts[1] == "attachments":
            raise PermissionError("附件是只读 logical reference，不提供物理产物路径或写入能力")
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
        session_nodes = [
            (node, self._path_resolver.resolve_session_node(node.node_id))
            for node in self._path_resolver.list_nodes()
            if node.kind == "session"
        ]
        session_nodes.sort(key=lambda item: len(item[1].parts), reverse=True)
        for node, session_root in session_nodes:
            try:
                tail = resolved.relative_to(session_root)
            except ValueError:
                continue
            return f"/{node.node_id}/{tail.as_posix()}"
        return super()._to_virtual_path(path)


def build_workspace_backend(
    workspace_root: Path,
    *, attachment_reader: Callable[[str, str], StoredAttachmentContent] | None = None,
) -> CompositeBackend:
    """构建统一的 DeepAgents 工作区后端，并隔离框架运行产物。"""
    workspace_files = FilesystemBackend(
        root_dir=str(workspace_root),
        virtual_mode=True,
    )
    session_artifacts = SessionArtifactBackend(workspace_root, attachment_reader=attachment_reader)
    routes = {
        SESSION_ARTIFACT_ROUTE: session_artifacts,
    }
    return CompositeBackend(
        default=workspace_files,
        routes=routes,
        artifacts_root=BOXTEAM_ARTIFACTS_ROOT,
    )
