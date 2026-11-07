"""目标 Python 读写已准入配置文件；Code OSS 继续拥有编辑、dirty 与 etag 语义。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import stat
import tempfile
from base64 import b64decode, b64encode
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from uuid import UUID

import jsonyx
import portalocker
from jsonyx.allow import COMMENTS, SURROGATES, TRAILING_COMMA

from app.core.atomic_fs import fsync_directory, native_io_path
from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.runtime.native_file_stat import birthtime_milliseconds, date_milliseconds
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationChangeDTO,
    ExtensionConfigurationEventDTO,
    ExtensionConfigurationReadDTO,
    ExtensionConfigurationStatDTO,
    ExtensionConfigurationWriteOptions,
)
from app.schemas.gateway.extension_host import (
    ExtensionHostConfigurationResourceDTO,
    ExtensionHostWorkspaceDTO,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    NativeFileStat,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_files.runtime import rename_entry_exclusive


def native_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    native = (f"//{parsed.netloc}" if parsed.netloc else "") + parsed.path
    if os.name == "nt":
        # TODO: 最低 Python 升至 3.13 后使用 Path.from_uri；旧标准库在解码前识别盘符。
        native = re.sub(r"^/([A-Za-z])%3[Aa](?=/|$)", r"/\1:", native)
    path = Path(url2pathname(native))
    if parsed.scheme != "file" or not path.is_absolute() or parsed.query or parsed.fragment:
        raise ValueError("资源不是当前目标机器的原生绝对 file URI")
    return path


@contextmanager
def native_file_errors() -> Iterator[None]:
    try:
        yield
    except OSError as error:
        code = {errno.ENOENT: "EntryNotFound", errno.EEXIST: "EntryExists", errno.ENOTDIR: "EntryNotADirectory",
                errno.EISDIR: "EntryIsADirectory", errno.EACCES: "NoPermissions", errno.EPERM: "NoPermissions"}.get(error.errno or 0, "Unknown")
        raise ExtensionManagementError(str(error), code) from error


def native_stat(path: Path) -> NativeFileStat:
    with native_file_errors():
        entry = native_io_path(path)
        metadata = entry.lstat()
        symbolic = stat.S_ISLNK(metadata.st_mode)
        dangling = False
        if symbolic:
            try:
                metadata = entry.stat()
            except FileNotFoundError:
                dangling = True
        kind = 0 if dangling else 1 if stat.S_ISREG(metadata.st_mode) else 2 if stat.S_ISDIR(metadata.st_mode) else 0
        permissions = (2 if not metadata.st_mode & stat.S_IWUSR else 0) | (4 if metadata.st_mode & 0o111 else 0)
        return NativeFileStat(type=kind | (64 if symbolic else 0), size=metadata.st_size,
            ctime=birthtime_milliseconds(entry, metadata, follow_symlinks=not dangling),
            mtime=date_milliseconds(metadata.st_mtime_ns), permissions=permissions or None)


def _uri_key(uri: str) -> tuple[str, str, str, str, str]:
    try:
        parsed = urlsplit(uri)
    except ValueError as error:
        raise ExtensionManagementError(f"稳定工作区视图的 folder uri 无效: {error}", "Unknown") from error
    path = unquote(parsed.path)
    return parsed.scheme, parsed.netloc, os.path.normcase(path if path.startswith("/") else "/" + path), parsed.query, parsed.fragment


def _validate_roots(resource: Path, content: bytes, roots: tuple[str, ...]) -> None:
    try:
        # 仅解析冻结根集合；不生成编辑、不归一化或重新序列化已有 JSONC 正文。
        value: object = jsonyx.loads(content.decode("utf-8-sig"), allow=COMMENTS | SURROGATES | TRAILING_COMMA,
            hooks={"int": float})
    except (ValueError, jsonyx.JSONSyntaxError) as error:
        raise ExtensionManagementError(f"稳定工作区视图配置不是有效 UTF-8 JSONC，已保留原文件: {error}", "Unknown") from error
    if not isinstance(value, dict) or not isinstance(value.get("folders"), list):
        raise ExtensionManagementError("稳定工作区视图配置损坏，已保留原文件", "Unknown")
    folders = cast(list[object], value["folders"])
    actual: list[tuple[str, str, str, str, str]] = []
    for item in folders:
        if not isinstance(item, dict):
            raise ExtensionManagementError("稳定工作区视图的 folders 条目无效", "Unknown")
        folder = cast(dict[str, object], item)
        if folder.get("name") and not isinstance(folder["name"], str):
            raise ExtensionManagementError("稳定工作区视图的 folder name 无效", "Unknown")
        relative, uri = folder.get("path"), folder.get("uri")
        if isinstance(relative, str) and relative:
            uri = Path(os.path.abspath(resource.parent / relative)).as_uri()
        elif isinstance(relative, str) or not isinstance(uri, str):
            raise ExtensionManagementError("稳定工作区视图的 folder path 或 uri 无效", "Unknown")
        actual.append(_uri_key(uri))
    if len(set(actual)) != len(actual) or actual != [_uri_key(root) for root in roots]:
        raise ExtensionManagementError("配置中的有序工作区根与已准入视图不符，必须重新打开明确视图", "Unknown")


def workspace_configuration_file(workspace_home_uri: str, workspace: ExtensionHostWorkspaceDTO) -> Path | None:
    if len(workspace.folders) < 2:
        return None
    if str(UUID(workspace.id)) != workspace.id.lower():
        raise ValueError("稳定工作区视图 ID 必须是规范 UUID")
    return native_path(workspace_home_uri) / workspace.id / "workspace.code-workspace"


def initialize_configuration_files(settings_uri: str, workspace_home_uri: str, workspace: ExtensionHostWorkspaceDTO) -> str | None:
    with native_file_errors():
        native_io_path(native_path(settings_uri).parent).mkdir(parents=True, exist_ok=True)
        resource = workspace_configuration_file(workspace_home_uri, workspace)
        if resource is None:
            return None
        target = native_io_path(resource)
        target.parent.mkdir(parents=True, exist_ok=True)
        roots = tuple(folder.native_uri for folder in workspace.folders)
        for root in roots:
            native_path(root)
        if target.exists():
            _validate_roots(resource, _read_bytes(resource), roots)
            return resource.as_uri()
        payload = (json.dumps({"folders": [{"uri": folder.native_uri, "name": folder.name}
            for folder in workspace.folders]}, ensure_ascii=False, indent="\t") + "\n").encode()
        descriptor, filename = tempfile.mkstemp(prefix=".workspace.code-workspace.", dir=target.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                rename_entry_exclusive(temporary, target)
                fsync_directory(target.parent)
            except FileExistsError:
                # 并发 Backend/窗口已完成首次发布；只校验胜者，绝不覆盖它的配置与注释。
                _validate_roots(resource, _read_bytes(resource), roots)
        finally:
            temporary.unlink(missing_ok=True)
        return resource.as_uri()


def _read_bytes(path: Path) -> bytes:
    with native_file_errors():
        descriptor = os.open(native_io_path(path), os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            portalocker.lock(stream, portalocker.LOCK_SH)
            return stream.read()


def _write_bytes(path: Path, content: bytes, options: ExtensionConfigurationWriteOptions) -> NativeFileStat:
    if options.atomic:
        raise ExtensionManagementError("配置 Provider 尚未提供原子写入能力", "Unknown")
    with native_file_errors():
        if not native_stat(path.parent).type & 2:
            raise ExtensionManagementError("配置文件父级不是原生目录", "Unknown")
        entry = native_io_path(path)
        if options.unlock:
            try:
                metadata = entry.stat()
            except FileNotFoundError:
                metadata = None
            if metadata is not None and not metadata.st_mode & stat.S_IWUSR:
                entry.chmod(metadata.st_mode | stat.S_IWUSR)
        flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
        if options.create:
            flags |= os.O_CREAT
        if not options.overwrite:
            if not options.create:
                if entry.exists():
                    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(path))
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
            flags |= os.O_EXCL
        if options.append:
            flags |= os.O_APPEND
        descriptor = os.open(entry, flags, 0o666)
        with os.fdopen(descriptor, "ab" if options.append else "wb") as stream:
            portalocker.lock(stream, portalocker.LOCK_EX)
            if not options.append:
                os.ftruncate(stream.fileno(), 0)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            return native_stat(path)


class ExtensionConfigurationFiles:
    def __init__(self, settings: ExtensionHostConfigurationResourceDTO, workspace: ExtensionHostWorkspaceDTO,
                 watcher: WorkspaceFileWatchService, stopping: asyncio.Event, *, lock: asyncio.Lock | None = None) -> None:
        resources = [settings]
        if workspace.configuration is not None:
            resources.append(workspace.configuration)
        self._view = workspace.configuration
        self._view_roots = tuple(folder.native_uri for folder in workspace.folders)
        self._files = {resource.resource_uri: resource.native_uri for resource in resources}
        self._parents: dict[str, str] = {}
        self._changes: dict[str, str] = {}
        for resource in resources:
            parsed = parse_resource_uri(resource.resource_uri)
            parent = resource_display_uri(scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                tail_segments=parsed.tail_segments[:-1], gateway_authority=parsed.gateway_authority)
            native = native_path(resource.native_uri)
            self._parents[parent] = native.parent.as_uri()
            self._changes[self._path_key(native)] = resource.resource_uri
        self._roots = tuple(dict.fromkeys(native_path(value).parent for value in self._files.values()))
        self._watcher, self._stopping = watcher, stopping
        self._lock = lock if lock is not None else asyncio.Lock()

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _native(self, resource_uri: str, *, parent: bool = False) -> str:
        if self._stopping.is_set():
            raise ValueError("配置资源所属目标已停止")
        native = self._files.get(resource_uri)
        if native is None and parent:
            native = self._parents.get(resource_uri)
        if native is None:
            raise ValueError("配置资源不属于该宿主的明确 profile 或工作区视图")
        return native

    async def stat(self, resource_uri: str) -> ExtensionConfigurationStatDTO:
        async with self._lock:
            result = await run_sync_shielded(native_stat, native_path(self._native(resource_uri, parent=True)))
            return ExtensionConfigurationStatDTO(resource_uri=resource_uri, **result.model_dump())

    async def read(self, resource_uri: str) -> ExtensionConfigurationReadDTO:
        async with self._lock:
            content = await run_sync_shielded(_read_bytes, native_path(self._native(resource_uri)))
            return ExtensionConfigurationReadDTO(resource_uri=resource_uri, content_base64=b64encode(content).decode())

    async def write(self, resource_uri: str, content_base64: str,
                    options: ExtensionConfigurationWriteOptions) -> ExtensionConfigurationStatDTO:
        async with self._lock:
            path, content = native_path(self._native(resource_uri)), b64decode(content_base64, validate=True)
            if self._view is not None and resource_uri == self._view.resource_uri:
                if options.append:
                    raise ExtensionManagementError("工作区视图配置不支持追加写入", "Unknown")
                _validate_roots(path, content, self._view_roots)
            # 原 FileService/TextFileService 已执行 modified-since 与 dirty 检查；不另建 hash 或保存基线。
            result = await run_sync_shielded(_write_bytes, path, content, options)
            return ExtensionConfigurationStatDTO(resource_uri=resource_uri, **result.model_dump())

    async def close(self) -> None:
        self._stopping.set()
        # 与准入共用同一锁；取消中的 worker 也须先结算，排队操作随后明确拒绝。
        async with self._lock:
            pass

    async def events(self) -> AsyncGenerator[ExtensionConfigurationEventDTO, None]:
        # 精确文件筛选留在订阅内；不同客户端共享同一直接父目录的原生监听。
        stream = self._watcher.subscribe_roots(self._roots, recursive=False, include_internal_paths=True)
        pending = asyncio.create_task(anext(stream))
        stopped = asyncio.create_task(self._stopping.wait())
        initialized = asyncio.create_task(self._watcher.wait_until_ready(
            self._roots, recursive=False,
        ))
        failure: BaseException | None = None
        try:
            ready, _ = await asyncio.wait({pending, stopped, initialized}, return_when=asyncio.FIRST_COMPLETED)
            if stopped in ready:
                return
            if pending in ready and pending.result().error is not None:
                yield ExtensionConfigurationEventDTO(changes=[], overflow=False, error=pending.result().error)
                return
            if initialized not in ready:
                ready, _ = await asyncio.wait({stopped, initialized}, return_when=asyncio.FIRST_COMPLETED)
                if stopped in ready:
                    return
            initialized.result()
            # 首次/重连只让原模型重核；不读取正文或推进任何保存基线。
            yield ExtensionConfigurationEventDTO(changes=[], overflow=True)
            while True:
                ready, _ = await asyncio.wait({pending, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if stopped in ready:
                    return
                batch = pending.result()
                if batch.error is not None:
                    yield ExtensionConfigurationEventDTO(changes=[], overflow=False, error=batch.error)
                    return
                pending = asyncio.create_task(anext(stream))
                changes = [ExtensionConfigurationChangeDTO(resource_uri=self._changes[self._path_key(Path(change.path))],
                    kind=change.kind) for change in batch.changes if self._path_key(Path(change.path)) in self._changes]
                if changes or batch.overflow:
                    yield ExtensionConfigurationEventDTO(changes=changes, overflow=batch.overflow)
        except BaseException as error:
            failure = error
            raise
        finally:
            async def close_stream() -> None:
                tasks = (pending, stopped, initialized)
                for task in tasks:
                    task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                await stream.aclose()
                errors = [result for result in results if isinstance(result, BaseException)
                          and not isinstance(result, (asyncio.CancelledError, StopAsyncIteration)) and result is not failure]
                if errors:
                    raise BaseExceptionGroup("配置监听启动或取消未完成", errors)

            await run_cleanup_shielded(close_stream)
