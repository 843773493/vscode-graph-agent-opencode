from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import heapq
import json
import mimetypes
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from app.core.path_utils import (
    get_runtime_workspace_root,
    get_user_workspace_root,
    safe_join,
)
from app.schemas.public_v2.workspace import (
    WorkspaceContextDTO,
    WorkspaceDTO,
    WorkspaceFileContentDTO,
    WorkspaceFileListDTO,
    WorkspaceFileNodeDTO,
    WorkspaceFileScope,
    WorkspaceIndexRebuildDTO,
    WorkspaceIndexStatusDTO,
)
from app.services.infrastructure.config_service import ConfigService

DEFAULT_WORKSPACE_FILE_LIMIT = 500
MAX_PREVIEW_FILE_BYTES = 1024 * 1024
TEXT_PREVIEW_BINARY_SAMPLE_BYTES = 8192


class WorkspaceFileConflictError(RuntimeError):
    """文件打开后已被其他写入者修改。"""


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
        scope: WorkspaceFileScope = "workspace",
        limit: int = DEFAULT_WORKSPACE_FILE_LIMIT,
        cursor: str | None = None,
    ) -> WorkspaceFileListDTO:
        target_path, display_path = self._resolve_file_tree_path(
            path,
            scope=scope,
            allow_empty=True,
        )
        if not target_path.exists():
            raise FileNotFoundError(f"文件树路径不存在: {display_path or '.'}")
        if not target_path.is_dir():
            raise NotADirectoryError(f"文件树路径不是目录: {display_path or '.'}")

        cursor_key = self._decode_directory_cursor(cursor) if cursor else None
        items, next_cursor = await asyncio.to_thread(
            self._scan_directory,
            target_path,
            display_path,
            scope,
            limit,
            cursor_key,
        )

        return WorkspaceFileListDTO(
            root_path=(
                self.root_path
                if scope == "workspace"
                else str(self._filesystem_root())
            ),
            path=display_path,
            items=items,
            truncated=next_cursor is not None,
            limit=limit,
            next_cursor=next_cursor,
        )

    def _scan_directory(
        self,
        target_path: Path,
        display_path: str,
        scope: WorkspaceFileScope,
        limit: int,
        cursor_key: tuple[bool, str, str] | None,
    ) -> tuple[list[WorkspaceFileNodeDTO], str | None]:
        def entry_key(entry: os.DirEntry[str]) -> tuple[bool, str, str]:
            return (
                not entry.is_dir(follow_symlinks=False),
                entry.name.lower(),
                entry.name,
            )

        with os.scandir(target_path) as directory_entries:
            entries = heapq.nsmallest(
                limit + 1,
                (
                    entry
                    for entry in directory_entries
                    if cursor_key is None or entry_key(entry) > cursor_key
                ),
                key=entry_key,
            )
        page = entries[:limit]
        next_cursor = (
            self._encode_directory_cursor(entry_key(page[-1]))
            if len(entries) > limit and page
            else None
        )
        return (
            [self._entry_to_file_node(entry, display_path, scope=scope) for entry in page],
            next_cursor,
        )

    @staticmethod
    def _encode_directory_cursor(key: tuple[bool, str, str]) -> str:
        raw = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_directory_cursor(cursor: str) -> tuple[bool, str, str]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode()
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("目录分页游标格式错误") from error
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not isinstance(value[0], bool)
            or not isinstance(value[1], str)
            or not isinstance(value[2], str)
        ):
            raise ValueError("目录分页游标内容错误")
        return value[0], value[1], value[2]

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

    @staticmethod
    def _filesystem_root() -> Path:
        return Path(Path.cwd().anchor or os.path.abspath(os.sep)).resolve()

    def _resolve_file_tree_path(
        self,
        path: str,
        *,
        scope: WorkspaceFileScope,
        allow_empty: bool,
    ) -> tuple[Path, str]:
        if scope == "workspace":
            relative_path = self._normalize_workspace_relative_path(path)
            if not allow_empty and not relative_path:
                raise ValueError("工作区文件路径不能为空")
            return (
                safe_join(self._workspace_root, relative_path)
                if relative_path
                else self._workspace_root,
                relative_path,
            )

        normalized = path.strip()
        if not normalized:
            if not allow_empty:
                raise ValueError("文件系统路径不能为空")
            target = self._filesystem_root()
        else:
            target = Path(normalized).expanduser()
            if not target.is_absolute():
                raise ValueError(f"文件系统路径必须是绝对路径: {path}")
            target = target.resolve()
        return target, str(target)

    def _entry_to_file_node(
        self,
        entry: os.DirEntry[str],
        parent_path: str,
        *,
        scope: WorkspaceFileScope,
    ) -> WorkspaceFileNodeDTO:
        is_directory = entry.is_dir(follow_symlinks=False)
        is_symlink = entry.is_symlink()
        stat_result = None if is_directory else entry.stat(follow_symlinks=False)
        relative_path = (
            str(Path(parent_path) / entry.name)
            if scope == "filesystem"
            else f"{parent_path}/{entry.name}" if parent_path else entry.name
        )

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
            size=None if stat_result is None else stat_result.st_size,
            modified_at=(
                None
                if stat_result is None
                else datetime.fromtimestamp(
                    stat_result.st_mtime,
                    timezone.utc,
                ).isoformat()
            ),
        )

    async def get_file_content(
        self,
        *,
        path: str,
        scope: WorkspaceFileScope = "workspace",
    ) -> WorkspaceFileContentDTO:
        target_path, relative_path = self._resolve_file_tree_path(
            path,
            scope=scope,
            allow_empty=False,
        )
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
            root_path=(self.root_path if scope == "workspace" else str(self._filesystem_root())),
            path=relative_path,
            name=target_path.name,
            content=content,
            language=self._guess_language(target_path.name),
            size=stat_result.st_size,
            modified_at=datetime.fromtimestamp(
                stat_result.st_mtime,
                timezone.utc,
            ).isoformat(),
            revision=self._content_revision(raw_content),
        )

    def resolve_raw_file(
        self,
        *,
        path: str,
        scope: WorkspaceFileScope = "workspace",
    ) -> tuple[Path, str]:
        target_path, relative_path = self._resolve_file_tree_path(
            path,
            scope=scope,
            allow_empty=False,
        )
        if not target_path.exists():
            raise FileNotFoundError(f"工作区文件不存在: {relative_path}")
        if not target_path.is_file():
            raise IsADirectoryError(f"工作区路径不是文件: {relative_path}")

        media_type, _ = mimetypes.guess_type(target_path.name)
        return target_path, media_type or "application/octet-stream"

    async def update_file_content(
        self,
        *,
        path: str,
        content: str,
        expected_revision: str,
        scope: WorkspaceFileScope = "workspace",
    ) -> WorkspaceFileContentDTO:
        target_path, relative_path = self._resolve_file_tree_path(
            path,
            scope=scope,
            allow_empty=False,
        )
        unresolved_path = (
            self._workspace_root.joinpath(relative_path)
            if scope == "workspace"
            else Path(path).expanduser()
        )
        if unresolved_path.is_symlink():
            raise ValueError(f"不允许通过文件预览编辑符号链接: {relative_path}")
        if not target_path.exists():
            raise FileNotFoundError(f"工作区文件不存在: {relative_path}")
        if not target_path.is_file():
            raise IsADirectoryError(f"工作区路径不是文件: {relative_path}")

        current_content = target_path.read_bytes()
        current_revision = self._content_revision(current_content)
        if current_revision != expected_revision:
            raise WorkspaceFileConflictError(
                f"文件已在编辑期间发生变化，请重新载入后再保存: {relative_path}"
            )
        if self._looks_like_binary(current_content):
            raise ValueError(f"文件不是可编辑文本: {relative_path}")

        has_utf8_bom = current_content.startswith(codecs.BOM_UTF8)
        encoded_content = content.encode("utf-8")
        if has_utf8_bom:
            encoded_content = codecs.BOM_UTF8 + encoded_content
        if len(encoded_content) > MAX_PREVIEW_FILE_BYTES:
            raise ValueError(
                f"文件过大，暂不允许保存: {relative_path} "
                f"({len(encoded_content)} bytes)"
            )
        if encoded_content == current_content:
            return await self.get_file_content(path=relative_path, scope=scope)

        original_mode = stat.S_IMODE(target_path.stat().st_mode)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target_path.parent,
                prefix=f".{target_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                temporary_file.write(encoded_content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            # TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
            if os.name != "nt":
                os.chmod(temporary_path, original_mode)
            latest_revision = self._content_revision(target_path.read_bytes())
            if latest_revision != expected_revision:
                raise WorkspaceFileConflictError(
                    f"文件在保存期间发生变化，请重新载入后再保存: {relative_path}"
                )
            os.replace(temporary_path, target_path)
            temporary_path = None
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)

        return await self.get_file_content(path=relative_path, scope=scope)

    async def create_file_entry(
        self,
        *,
        directory_path: str,
        scope: WorkspaceFileScope,
        name: str,
        kind: str,
    ) -> WorkspaceFileListDTO:
        target_directory, display_path = self._resolve_file_tree_path(
            directory_path,
            scope=scope,
            allow_empty=True,
        )
        if not target_directory.exists():
            raise FileNotFoundError(f"目标目录不存在: {display_path or '.'}")
        if not target_directory.is_dir():
            raise NotADirectoryError(f"目标路径不是目录: {display_path or '.'}")
        entry_name = self._validate_entry_name(name)
        target = target_directory / entry_name
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"目标已存在: {target}")
        if kind == "directory":
            target.mkdir()
        elif kind == "file":
            with target.open("x", encoding="utf-8"):
                pass
        else:
            raise ValueError(f"不支持的文件条目类型: {kind}")
        return await self.list_files(path=display_path, scope=scope)

    async def paste_file_entries(
        self,
        *,
        directory_path: str,
        scope: WorkspaceFileScope,
        source_paths: list[str],
    ) -> WorkspaceFileListDTO:
        target_directory, display_path = self._resolve_file_tree_path(
            directory_path,
            scope=scope,
            allow_empty=True,
        )
        if not target_directory.exists():
            raise FileNotFoundError(f"目标目录不存在: {display_path or '.'}")
        if not target_directory.is_dir():
            raise NotADirectoryError(f"目标路径不是目录: {display_path or '.'}")

        copy_pairs: list[tuple[Path, Path]] = []
        destinations: set[Path] = set()
        for raw_source_path in source_paths:
            source = Path(raw_source_path).expanduser()
            if not source.is_absolute():
                raise ValueError(f"粘贴来源必须是绝对路径: {raw_source_path}")
            if source.is_symlink():
                raise ValueError(f"暂不支持粘贴符号链接: {source}")
            source = source.resolve()
            if not source.exists():
                raise FileNotFoundError(f"粘贴来源不存在: {source}")
            if not source.is_file() and not source.is_dir():
                raise ValueError(f"粘贴来源不是文件或目录: {source}")
            if not source.name:
                raise ValueError(f"不能粘贴文件系统根目录: {source}")
            if source.is_dir() and target_directory.is_relative_to(source):
                raise ValueError(f"不能把目录粘贴到自身内部: {source}")
            destination = target_directory / source.name
            if destination in destinations:
                raise FileExistsError(f"多个粘贴来源具有相同名称: {source.name}")
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"粘贴目标已存在: {destination}")
            destinations.add(destination)
            copy_pairs.append((source, destination))

        for source, destination in copy_pairs:
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        return await self.list_files(path=display_path, scope=scope)

    async def copy_file_entry(
        self,
        *,
        directory_path: str,
        scope: WorkspaceFileScope,
        source_path: str,
        source_scope: WorkspaceFileScope,
    ) -> WorkspaceFileListDTO:
        source, source_display_path = self._resolve_file_tree_path(
            source_path,
            scope=source_scope,
            allow_empty=False,
        )
        if source.is_symlink():
            raise ValueError(f"暂不支持复制符号链接: {source_display_path}")
        if not source.exists():
            raise FileNotFoundError(f"复制来源不存在: {source_display_path}")
        if not source.is_file() and not source.is_dir():
            raise ValueError(f"复制来源不是文件或目录: {source_display_path}")

        target_directory, display_path = self._resolve_file_tree_path(
            directory_path,
            scope=scope,
            allow_empty=True,
        )
        if not target_directory.exists():
            raise FileNotFoundError(f"目标目录不存在: {display_path or '.'}")
        if not target_directory.is_dir():
            raise NotADirectoryError(f"目标路径不是目录: {display_path or '.'}")
        if source.is_dir() and target_directory.is_relative_to(source):
            raise ValueError(f"不能把目录复制到自身内部: {source_display_path}")

        destination = target_directory / source.name
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"复制目标已存在: {destination}")

        def copy_entry() -> None:
            staging_root = Path(
                tempfile.mkdtemp(prefix=f".{source.name}-copy-", dir=target_directory)
            )
            staged = staging_root / source.name
            try:
                if source.is_dir():
                    shutil.copytree(source, staged)
                else:
                    shutil.copy2(source, staged)
                os.replace(staged, destination)
            finally:
                shutil.rmtree(staging_root, ignore_errors=True)

        await asyncio.to_thread(copy_entry)
        return await self.list_files(path=display_path, scope=scope)

    async def upload_file_entries(
        self,
        *,
        directory_path: str,
        scope: WorkspaceFileScope,
        entries: list[tuple[str, BinaryIO]],
    ) -> WorkspaceFileListDTO:
        target_directory, display_path = self._resolve_file_tree_path(
            directory_path,
            scope=scope,
            allow_empty=True,
        )
        if not target_directory.exists():
            raise FileNotFoundError(f"上传目标目录不存在: {display_path or '.'}")
        if not target_directory.is_dir():
            raise NotADirectoryError(f"上传目标路径不是目录: {display_path or '.'}")
        if not entries:
            raise ValueError("上传内容不能为空")

        destinations: list[tuple[Path, BinaryIO]] = []
        unique_destinations: set[Path] = set()
        for relative_path, stream in entries:
            normalized = relative_path.replace("\\", "/").strip("/")
            parts = normalized.split("/") if normalized else []
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise ValueError(f"上传文件相对路径无效: {relative_path}")
            destination = safe_join(target_directory, normalized)
            if destination in unique_destinations:
                raise FileExistsError(f"上传内容包含重复路径: {normalized}")
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"上传目标已存在: {destination}")
            unique_destinations.add(destination)
            destinations.append((destination, stream))

        created_files: list[Path] = []
        created_directories: list[Path] = []

        def write_entries() -> None:
            try:
                for destination, stream in destinations:
                    missing_parents: list[Path] = []
                    parent = destination.parent
                    while parent != target_directory and not parent.exists():
                        missing_parents.append(parent)
                        parent = parent.parent
                    for missing_parent in reversed(missing_parents):
                        missing_parent.mkdir()
                        created_directories.append(missing_parent)
                    with destination.open("xb") as output:
                        created_files.append(destination)
                        shutil.copyfileobj(stream, output)
            except BaseException:
                for created_file in reversed(created_files):
                    created_file.unlink(missing_ok=True)
                for created_directory in reversed(created_directories):
                    created_directory.rmdir()
                raise

        await asyncio.to_thread(write_entries)
        return await self.list_files(path=display_path, scope=scope)

    def prepare_file_download(
        self,
        *,
        path: str,
        scope: WorkspaceFileScope,
    ) -> tuple[Path, str, str, bool]:
        target, display_path = self._resolve_file_tree_path(
            path,
            scope=scope,
            allow_empty=False,
        )
        if target.is_symlink():
            raise ValueError(f"暂不支持下载符号链接: {display_path}")
        if not target.exists():
            raise FileNotFoundError(f"下载路径不存在: {display_path}")
        if target.is_file():
            media_type, _ = mimetypes.guess_type(target.name)
            return target, target.name, media_type or "application/octet-stream", False
        if not target.is_dir():
            raise ValueError(f"下载路径不是文件或目录: {display_path}")

        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f"{target.name or 'workspace'}-",
            suffix=".zip",
        )
        os.close(temporary_fd)
        temporary_path = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for child in sorted(target.rglob("*")):
                    if child.is_symlink():
                        raise ValueError(f"下载目录包含暂不支持的符号链接: {child}")
                    archive_name = Path(target.name) / child.relative_to(target)
                    archive.write(child, archive_name)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return (
            temporary_path,
            f"{target.name or 'workspace'}.zip",
            "application/zip",
            True,
        )

    def reveal_file_entry(
        self,
        *,
        path: str,
        scope: WorkspaceFileScope,
    ) -> Path:
        target, display_path = self._resolve_file_tree_path(
            path,
            scope=scope,
            allow_empty=True,
        )
        if not target.exists():
            raise FileNotFoundError(f"系统定位路径不存在: {display_path or '.'}")

        if sys.platform == "darwin":
            command = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
        elif os.name == "nt":
            command = (
                ["explorer.exe", f"/select,{target}"]
                if target.is_file()
                else ["explorer.exe", str(target)]
            )
        else:
            opener = shutil.which("gio") or shutil.which("xdg-open")
            if opener is None:
                raise RuntimeError("当前系统未安装 gio 或 xdg-open，无法在系统文件管理器中显示")
            command = (
                [opener, "open", str(target if target.is_dir() else target.parent)]
                if Path(opener).name == "gio"
                else [opener, str(target if target.is_dir() else target.parent)]
            )
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return target

    @staticmethod
    def _validate_entry_name(name: str) -> str:
        normalized = name.strip()
        if normalized != name or normalized in {"", ".", ".."}:
            raise ValueError(f"文件名无效: {name!r}")
        if "/" in normalized or "\\" in normalized or "\x00" in normalized:
            raise ValueError(f"文件名只能包含一个路径片段: {name!r}")
        return normalized

    @staticmethod
    def _content_revision(raw_content: bytes) -> str:
        return hashlib.sha256(raw_content).hexdigest()

    def _looks_like_binary(self, raw_content: bytes) -> bool:
        sample = raw_content[:TEXT_PREVIEW_BINARY_SAMPLE_BYTES]
        return b"\x00" in sample

    def _guess_language(self, filename: str) -> str:
        if filename in LANGUAGE_BY_FILENAME:
            return LANGUAGE_BY_FILENAME[filename]
        _, extension = os.path.splitext(filename)
        return LANGUAGE_BY_EXTENSION.get(extension.lower(), "plaintext")
