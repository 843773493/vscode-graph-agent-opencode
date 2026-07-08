from __future__ import annotations

import os
from datetime import datetime, timezone

from app.core.path_utils import get_runtime_workspace_root, get_user_workspace_root, safe_join
from app.schemas.public_v2.workspace import (
    WorkspaceContextDTO,
    WorkspaceDTO,
    WorkspaceFileContentDTO,
    WorkspaceFileListDTO,
    WorkspaceFileNodeDTO,
    WorkspaceIndexRebuildDTO,
    WorkspaceIndexStatusDTO,
)
from app.services.infrastructure.config_service import ConfigService


DEFAULT_WORKSPACE_FILE_LIMIT = 500
MAX_PREVIEW_FILE_BYTES = 1024 * 1024
TEXT_PREVIEW_BINARY_SAMPLE_BYTES = 8192


LANGUAGE_BY_EXTENSION = {
    ".css": "css",
    ".html": "html",
    ".js": "javascript",
    ".json": "json",
    ".jsonc": "jsonc",
    ".jsx": "javascript",
    ".md": "markdown",
    ".mjs": "javascript",
    ".py": "python",
    ".sh": "shell",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "plaintext",
    ".yaml": "yaml",
    ".yml": "yaml",
}

LANGUAGE_BY_FILENAME = {
    ".env": "dotenv",
    ".env.example": "dotenv",
    ".gitignore": "ignore",
    "AGENTS.md": "markdown",
    "README.md": "markdown",
}


class WorkspaceService:
    def __init__(self, *, config_service: ConfigService):
        self.workspace_id = "ws_local"
        self._workspace_root = get_runtime_workspace_root()
        self.root_path = str(self._workspace_root)
        self.user_workspace_root = str(get_user_workspace_root())
        self.name = os.path.basename(self.root_path)
        self._config_service = config_service

    async def get(self) -> WorkspaceDTO:
        return WorkspaceDTO(
            workspace_id=self.workspace_id,
            root_path=self.root_path,
            name=self.name,
            project_type="python",
            git={
                "enabled": False,
                "root": self.root_path,
                "branch": "main"
            },
            runtime={
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat() + "Z"
            }
        )

    async def get_context(self) -> WorkspaceContextDTO:
        return WorkspaceContextDTO(
            workspace_id=self.workspace_id,
            root_path=self.root_path,
            project_type="python",
            languages=["python", "javascript", "typescript"],
            git={},
            index_status={"status": "ready", "indexed_at": datetime.now(timezone.utc).isoformat() + "Z"},
            config={}
        )

    async def get_index_status(self) -> WorkspaceIndexStatusDTO:
        return WorkspaceIndexStatusDTO(
            status="ready",
            indexed_files=0,
            last_updated=datetime.now(timezone.utc).isoformat() + "Z",
        )

    async def rebuild_index(self) -> WorkspaceIndexRebuildDTO:
        return WorkspaceIndexRebuildDTO(
            status="started",
            job_id="index_001",
        )

    async def list_files(
        self,
        *,
        path: str = "",
        limit: int = DEFAULT_WORKSPACE_FILE_LIMIT,
    ) -> WorkspaceFileListDTO:
        relative_path = self._normalize_workspace_relative_path(path)
        target_path = (
            safe_join(self._workspace_root, relative_path)
            if relative_path
            else self._workspace_root
        )
        if not target_path.exists():
            raise FileNotFoundError(f"工作区路径不存在: {relative_path or '.'}")
        if not target_path.is_dir():
            raise NotADirectoryError(f"工作区路径不是目录: {relative_path or '.'}")

        items: list[WorkspaceFileNodeDTO] = []
        entries = list(os.scandir(target_path))
        entries.sort(
            key=lambda entry: (
                not entry.is_dir(follow_symlinks=False),
                entry.name.lower(),
                entry.name,
            )
        )

        for entry in entries[:limit]:
            items.append(self._entry_to_file_node(entry, relative_path))

        return WorkspaceFileListDTO(
            root_path=self.root_path,
            path=relative_path,
            items=items,
            truncated=len(entries) > limit,
            limit=limit,
        )

    def _normalize_workspace_relative_path(self, path: str) -> str:
        normalized = path.replace("\\", "/").strip()
        if normalized.startswith("/"):
            raise ValueError(f"工作区文件路径必须是相对路径: {path}")

        parts: list[str] = []
        for part in normalized.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                raise ValueError(f"工作区文件路径不能包含上级目录: {path}")
            parts.append(part)

        return "/".join(parts)

    def _entry_to_file_node(
        self,
        entry: os.DirEntry[str],
        parent_path: str,
    ) -> WorkspaceFileNodeDTO:
        stat_result = entry.stat(follow_symlinks=False)
        is_directory = entry.is_dir(follow_symlinks=False)
        is_symlink = entry.is_symlink()
        relative_path = f"{parent_path}/{entry.name}" if parent_path else entry.name

        if is_symlink:
            kind = "symlink"
        elif is_directory:
            kind = "directory"
        elif entry.is_file(follow_symlinks=False):
            kind = "file"
        else:
            kind = "other"

        return WorkspaceFileNodeDTO(
            name=entry.name,
            path=relative_path,
            kind=kind,
            has_children=is_directory,
            size=None if is_directory else stat_result.st_size,
            modified_at=datetime.fromtimestamp(
                stat_result.st_mtime,
                timezone.utc,
            ).isoformat(),
        )

    async def get_file_content(self, *, path: str) -> WorkspaceFileContentDTO:
        relative_path = self._normalize_workspace_relative_path(path)
        if not relative_path:
            raise ValueError("文件预览路径不能为空")

        target_path = safe_join(self._workspace_root, relative_path)
        if not target_path.exists():
            raise FileNotFoundError(f"工作区文件不存在: {relative_path}")
        if not target_path.is_file():
            raise IsADirectoryError(f"工作区路径不是文件: {relative_path}")

        stat_result = target_path.stat()
        if stat_result.st_size > MAX_PREVIEW_FILE_BYTES:
            raise ValueError(
                f"文件过大，暂不预览: {relative_path} ({stat_result.st_size} bytes)"
            )

        raw_content = target_path.read_bytes()
        if self._looks_like_binary(raw_content):
            raise ValueError(f"文件不是可预览文本: {relative_path}")

        try:
            content = raw_content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"文件不是 UTF-8 文本，暂不预览: {relative_path}") from error

        return WorkspaceFileContentDTO(
            root_path=self.root_path,
            path=relative_path,
            name=target_path.name,
            content=content,
            language=self._guess_language(target_path.name),
            size=stat_result.st_size,
            modified_at=datetime.fromtimestamp(
                stat_result.st_mtime,
                timezone.utc,
            ).isoformat(),
        )

    def _looks_like_binary(self, raw_content: bytes) -> bool:
        sample = raw_content[:TEXT_PREVIEW_BINARY_SAMPLE_BYTES]
        return b"\x00" in sample

    def _guess_language(self, filename: str) -> str:
        if filename in LANGUAGE_BY_FILENAME:
            return LANGUAGE_BY_FILENAME[filename]
        _, extension = os.path.splitext(filename)
        return LANGUAGE_BY_EXTENSION.get(extension.lower(), "plaintext")
