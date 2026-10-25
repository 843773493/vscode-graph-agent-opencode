from __future__ import annotations

import asyncio
import codecs
import heapq
import mimetypes
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from app.core.hashing import sha256_hex
from app.core.path_utils import (
    get_user_workspace_root,
    get_workspace_root,
)
from app.core.resource_uri import file_uri_child, file_uri_root
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.workspace import (
    WorkspaceContextDTO,
    WorkspaceDTO,
    WorkspaceFileContentDTO,
    WorkspaceFileReferencesDTO,
    WorkspaceFileRootsDTO,
    WorkspaceFileScope,
    WorkspaceIndexRebuildDTO,
    WorkspaceIndexStatusDTO,
)
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryKeyDTO,
    WorkspaceFileListDTO,
    WorkspaceFileMutationIntentDTO,
    WorkspaceFileNodeDTO,
    WorkspaceFileOperationReceiptDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_files.filesystem import (
    PreparedFileDownload,
    file_download_access,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)
from app.services.infrastructure.workspace_files.operations import (
    WorkspaceFileOperations,
)
from app.services.infrastructure.workspace_files.paths import (
    AttachmentDirectoryPaths,
    NativeDirectoryBindings,
    WorkspaceFilePaths,
)
from app.services.infrastructure.workspace_files.store import (
    WorkspaceFileOperationConflictError,
    decode_directory_cursor,
    digest,
    encode_directory_cursor,
)
from app.services.infrastructure.workspace_files.text_publication import (
    FilePreimage,
    WorkspaceFileConflictError,
    publish_file_content,
    read_file_preimage,
)

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
    def __init__(
        self,
        *,
        config_service: ConfigService,
        workspace_root: Path | None = None,
        native_directories: NativeDirectoryBindings | None = None,
        attachments: AttachmentDirectoryPaths | None = None,
    ):
        self._workspace_root = (
            workspace_root or get_workspace_root()
        ).expanduser().resolve()
        self._workspace_id: str | None = None
        self.root_path = str(self._workspace_root)
        self.user_workspace_root = str(get_user_workspace_root())
        self.name = os.path.basename(self.root_path)
        self._config_service = config_service
        self._file_operations: WorkspaceFileOperations | None = None
        self.file_paths = WorkspaceFilePaths(workspace=self._workspace_root, workspace_id=self.workspace_id, bindings=native_directories, attachments=attachments)

    @property
    def file_operations(self) -> WorkspaceFileOperations:
        if self._file_operations is None:
            self._file_operations = WorkspaceFileOperations(
                workspace=self._workspace_root, workspace_id=self.workspace_id,
                resolve=self.file_paths.resolve,
                validate_name=self._validate_entry_name, observe=self._directory_observation,
                snapshot=self._operation_snapshot,
                paths=self.file_paths,
            )
        return self._file_operations

    async def start(self) -> None:
        await self.file_operations.start()

    async def stop(self) -> None:
        if self._file_operations is not None:
            await self._file_operations.stop()

    def close(self) -> None:
        if self._file_operations is not None:
            self._file_operations.close()

    @property
    def workspace_id(self) -> str:
        if self._workspace_id is None:
            self._workspace_id = load_or_create_workspace_id(self._workspace_root)
        return self._workspace_id

    async def get(self) -> WorkspaceDTO:
        return WorkspaceDTO(
            workspace_id=self.workspace_id,
            name=self.name,
            project_type="python",
            git={
                "enabled": False,
                "branch": "main"
            },
            runtime={
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat() + "Z"
            }
        )

    async def get_context(self) -> WorkspaceContextDTO:
        return WorkspaceContextDTO(
            workspace_id=self.workspace_id,
            project_type="python",
            languages=["python", "javascript", "typescript"],
            git={},
            index_status={"status": "ready", "indexed_at": datetime.now(UTC).isoformat() + "Z"},
            config={}
        )

    async def get_index_status(self) -> WorkspaceIndexStatusDTO:
        return WorkspaceIndexStatusDTO(
            status="ready",
            indexed_files=0,
            last_updated=datetime.now(UTC).isoformat() + "Z",
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
        limit: int | None = None,
        cursor: str | None = None,
    ) -> WorkspaceFileListDTO:
        resolved_limit = (
            self._config_service.get_workspace_file_default_limit()
            if limit is None
            else limit
        )
        target_path, display_path = await asyncio.to_thread(
            self.file_paths.resolve,
            path,
            scope=scope,
            allow_empty=True,
        )
        if not target_path.exists():
            raise FileNotFoundError(f"文件树路径不存在: {display_path or '.'}")
        if not target_path.is_dir():
            raise NotADirectoryError(f"文件树路径不是目录: {display_path or '.'}")

        owner = self.file_operations
        key, _ = await asyncio.to_thread(owner.directory_key, display_path, scope)
        cursor_value = decode_directory_cursor(cursor) if cursor else None
        cursor_key = None if cursor_value is None else cursor_value["after"]
        def snapshot():
            with owner.observation_lock:
                items, next_key, signature = self._scan_directory(
                    target_path, display_path, scope, resolved_limit, cursor_key,
                )
                revision = owner.store.observe([(key, key.path, signature, True)])[0].directory_revision
                return items, next_key, revision
        items, next_key, revision = await asyncio.to_thread(snapshot)
        if cursor_value is not None and (cursor_value["directory_key"] != key.model_dump() or cursor_value["revision"] != revision):
            raise WorkspaceFileOperationConflictError("目录分页游标的原 revision 已失效，请重新读取目录")
        next_cursor = None if next_key is None else encode_directory_cursor(key, revision, next_key)

        return WorkspaceFileListDTO(
            root_path=key.root,
            path=display_path,
            items=items,
            truncated=next_cursor is not None,
            limit=resolved_limit,
            next_cursor=next_cursor,
            directory_key=key,
            directory_revision=revision,
        )

    async def file_roots(self) -> WorkspaceFileRootsDTO:
        native, filesystem_uri = await asyncio.to_thread(self.file_paths.resolve, "", scope="filesystem")
        return WorkspaceFileRootsDTO(workspace_uri=self.file_paths.workspace_uri,
                                     filesystem_uri=filesystem_uri, filesystem_label=native.anchor)

    async def file_references(self, paths: list[str]) -> WorkspaceFileReferencesDTO:
        def resolve():
            return [self.file_paths.resolve(path, allow_empty=False)[1] for path in paths]
        return WorkspaceFileReferencesDTO(uris=await asyncio.to_thread(resolve))

    def _scan_directory(
        self,
        target_path: Path,
        display_path: str,
        scope: WorkspaceFileScope,
        limit: int,
        cursor_key: tuple[bool, str, str] | None,
    ) -> tuple[list[WorkspaceFileNodeDTO], tuple[bool, str, str] | None, str]:
        def entry_key(entry: WorkspaceFileNodeDTO) -> tuple[bool, str, str]:
            return (
                entry.kind != "directory",
                entry.name.lower(),
                entry.name,
            )

        fingerprint = 0
        count = 0
        staging = self._file_operations.staging_roots() if self._file_operations is not None else set()
        def observed_entries(directory_entries):
            nonlocal fingerprint, count
            for entry in directory_entries:
                if Path(entry.path) in staging or self.file_paths.is_private(Path(entry.path), root_uri=display_path):
                    continue
                node = self._entry_to_file_node(entry, display_path, scope=scope)
                fingerprint ^= int(digest({"name": node.name, "kind": node.kind, "size": node.size,
                                          "modified_at": node.modified_at}), 16)
                count += 1
                if cursor_key is None or entry_key(node) > cursor_key:
                    yield node
        with os.scandir(target_path) as directory_entries:
            entries = heapq.nsmallest(
                limit + 1,
                observed_entries(directory_entries),
                key=entry_key,
            )
        page = entries[:limit]
        next_cursor = (
            entry_key(page[-1])
            if len(entries) > limit and page
            else None
        )
        return (
            page,
            next_cursor,
            digest({"count": count, "members": f"{fingerprint:064x}"}),
        )

    def _directory_observation(self, key: WorkspaceDirectoryKeyDTO, target: Path):
        if target.is_symlink() or target.resolve() != target or not target.is_dir():
            return key, key.path, digest({"directory_missing": True}), False
        _items, _cursor, signature = self._scan_directory(target, key.path, key.scope, 0, None)
        return key, key.path, signature, True

    def _operation_snapshot(self, key: WorkspaceDirectoryKeyDTO, target: Path):
        limit = self._config_service.get_workspace_file_default_limit()
        items, after, signature = self._scan_directory(target, key.path, key.scope, limit, None)
        return {"root_path": key.root,
                "path": key.path, "items": [item.model_dump() for item in items],
                "limit": limit, "after": after}, signature

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
        relative_path = file_uri_child(parent_path, entry.name)

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
                    UTC,
                ).isoformat()
            ),
        )

    async def get_file_content(
        self,
        *,
        path: str,
        scope: WorkspaceFileScope = "workspace",
    ) -> WorkspaceFileContentDTO:
        target_path, relative_path = await asyncio.to_thread(
            self.file_paths.resolve,
            path,
            scope=scope,
            allow_empty=False,
        )
        observed = await asyncio.to_thread(read_file_preimage, target_path, reference=relative_path,
                                           max_bytes=self._config_service.get_workspace_preview_max_bytes())
        return self._file_content_dto(target_path, relative_path, observed)

    def _file_content_dto(self, target_path: Path, reference: str, observed: FilePreimage) -> WorkspaceFileContentDTO:
        raw_content, metadata = observed.content, observed.metadata
        max_preview_bytes = self._config_service.get_workspace_preview_max_bytes()
        if len(raw_content) > max_preview_bytes:
            raise ValueError(f"文件过大，暂不预览: {reference} ({len(raw_content)} bytes)")
        if self._looks_like_binary(raw_content):
            raise ValueError(f"文件不是可预览文本: {reference}")
        try:
            content = raw_content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"文件不是 UTF-8 文本，暂不预览: {reference}") from error
        return WorkspaceFileContentDTO(
            root_path=file_uri_root(reference), path=reference, name=target_path.name,
            content=content, language=self._guess_language(target_path.name), size=len(raw_content),
            modified_at=datetime.fromtimestamp(metadata.st_mtime, UTC).isoformat(),
            revision=sha256_hex(raw_content),
        )

    async def update_file_content(
        self,
        *,
        path: str,
        content: str,
        expected_revision: str,
        scope: WorkspaceFileScope = "workspace",
    ) -> WorkspaceFileContentDTO:
        return await asyncio.to_thread(self._update_file_content, path, content, expected_revision, scope)

    def _update_file_content(self, path: str, content: str, expected_revision: str,
                             scope: WorkspaceFileScope) -> WorkspaceFileContentDTO:
        with workspace_file_mutation_lock(self._workspace_root):
            target_path, reference = self.file_paths.resolve(path, scope=scope, allow_empty=False, reject_symlink=True, for_write=True)
            observed = read_file_preimage(target_path, reference=reference,
                                          max_bytes=self._config_service.get_workspace_preview_max_bytes())
            if sha256_hex(observed.content) != expected_revision:
                raise WorkspaceFileConflictError(f"文件已在编辑期间发生变化，请重新载入后再保存: {reference}")
            # 先核原件的既有文本/预算能力；不把媒体或无效编码转换成文本。
            self._file_content_dto(target_path, reference, observed)
            encoded = content.encode("utf-8")
            if observed.content.startswith(codecs.BOM_UTF8):
                encoded = codecs.BOM_UTF8 + encoded
            if len(encoded) > self._config_service.get_workspace_preview_max_bytes():
                raise ValueError(f"文件过大，暂不允许保存: {reference} ({len(encoded)} bytes)")
            if encoded != observed.content:
                observed = publish_file_content(target_path, encoded, before=observed, reference=reference)
            return self._file_content_dto(target_path, reference, observed)

    async def create_file_entry(self, *, directory_path: str, scope: WorkspaceFileScope,
                                name: str, kind: str, intent: WorkspaceFileMutationIntentDTO,
                                actor: str) -> WorkspaceFileOperationReceiptDTO:
        return await self.file_operations.enqueue(kind="create", directory_path=directory_path, scope=scope,
                                                  intent=intent, actor=actor, data={"name": name, "kind": kind})

    async def paste_file_entries(self, *, directory_path: str, scope: WorkspaceFileScope,
                                 source_paths: list[str], intent: WorkspaceFileMutationIntentDTO,
                                 actor: str) -> WorkspaceFileOperationReceiptDTO:
        return await self.file_operations.enqueue(kind="paste", directory_path=directory_path, scope=scope,
                                                  intent=intent, actor=actor, data={"source_paths": source_paths})

    async def copy_file_entry(self, *, directory_path: str, scope: WorkspaceFileScope,
                              source_path: str, source_scope: WorkspaceFileScope,
                              intent: WorkspaceFileMutationIntentDTO, actor: str) -> WorkspaceFileOperationReceiptDTO:
        return await self.file_operations.enqueue(kind="copy", directory_path=directory_path, scope=scope,
                                                  intent=intent, actor=actor,
                                                  data={"source_path": source_path, "source_scope": source_scope})

    async def upload_file_entries(self, *, directory_path: str, scope: WorkspaceFileScope,
                                  entries: list[tuple[str, BinaryIO]], intent: WorkspaceFileMutationIntentDTO,
                                  actor: str) -> WorkspaceFileOperationReceiptDTO:
        return await self.file_operations.enqueue(kind="upload", directory_path=directory_path, scope=scope,
                                                  intent=intent, actor=actor, data={}, uploads=entries)

    def prepare_file_download(
        self,
        *,
        path: str,
        scope: WorkspaceFileScope = "workspace",
        archive_directory: bool = True,
    ) -> PreparedFileDownload:
        with file_download_access(None, "目标解析"):
            target, display_path = self.file_paths.resolve(
                path,
                scope=scope,
                allow_empty=False,
            )
        with file_download_access(display_path, "文件检查"):
            if target.is_symlink():
                raise ValueError(f"暂不支持下载符号链接: {display_path}")
            metadata = target.stat()
        if stat.S_ISREG(metadata.st_mode):
            media_type, _ = mimetypes.guess_type(target.name)
            return PreparedFileDownload(target, display_path, target.name,
                                        media_type or "application/octet-stream", metadata)
        if not archive_directory:
            raise IsADirectoryError(f"工作区路径不是文件: {display_path}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"下载路径不是文件或目录: {display_path}")

        with file_download_access(display_path, "临时文件创建"):
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f"{target.name or 'workspace'}-",
                suffix=".zip",
            )
        temporary_path = Path(temporary_name)
        try:
            with file_download_access(display_path, "目录归档"), os.fdopen(temporary_fd, "w+b") as output, zipfile.ZipFile(
                output,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                def add_children(directory: Path) -> None:
                    for child in sorted(directory.iterdir()):
                        self.file_paths.require_access(child, root_uri=display_path)
                        if child.is_symlink():
                            raise ValueError(f"下载目录包含暂不支持的符号链接: {file_uri_child(display_path, *child.relative_to(target).parts)}")
                        archive.write(child, Path(target.name) / child.relative_to(target))
                        if child.is_dir():
                            add_children(child)
                add_children(target)
            with file_download_access(display_path, "归档检查"):
                metadata = temporary_path.stat()
        except BaseException as error:
            try:
                with file_download_access(display_path, "临时文件清理"):
                    temporary_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise BaseExceptionGroup("目录下载准备失败且临时文件清理失败", [error, cleanup_error]) from None
            raise
        return PreparedFileDownload(
            temporary_path,
            display_path,
            f"{target.name or 'workspace'}.zip",
            "application/zip",
            metadata,
            temporary=True,
        )

    def reveal_file_entry(
        self,
        *,
        path: str,
        scope: WorkspaceFileScope,
    ) -> str:
        target, display_path = self.file_paths.resolve(
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
        process_group_options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **process_group_options,
        )
        return display_path

    @staticmethod
    def _validate_entry_name(name: str) -> str:
        normalized = name.strip()
        if normalized != name or normalized in {"", ".", ".."}:
            raise ValueError("文件名无效：名称不能为空、点段或包含首尾空白")
        if "/" in normalized or "\\" in normalized or "\x00" in normalized:
            raise ValueError("文件名只能包含一个不含 NUL 的路径片段")
        return normalized

    def _looks_like_binary(self, raw_content: bytes) -> bool:
        sample_size = self._config_service.get_workspace_preview_binary_sample_bytes()
        sample = raw_content[:sample_size]
        return b"\x00" in sample

    def _guess_language(self, filename: str) -> str:
        if filename in LANGUAGE_BY_FILENAME:
            return LANGUAGE_BY_FILENAME[filename]
        _, extension = os.path.splitext(filename)
        return LANGUAGE_BY_EXTENSION.get(extension.lower(), "plaintext")
