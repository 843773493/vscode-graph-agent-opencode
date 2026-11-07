"""已准入宿主的两个原生配置资源；原管理 Node 读写，共享 watcher 只报告变化。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.core.shielded_cleanup import run_cleanup_shielded
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationChangeDTO,
    ExtensionConfigurationEventDTO,
    ExtensionConfigurationReadDTO,
    ExtensionConfigurationStatDTO,
    ExtensionConfigurationWriteOptions,
)
from app.schemas.gateway.extension_host import ExtensionHostInitializationDTO
from app.services.infrastructure.extension_host.management import (
    NativeConfigurationFolder,
    NativeConfigurationView,
    NodeExtensionManagement,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChange,
    WorkspaceFileWatchService,
)


def _native_path(uri: str) -> Path:
    parsed = urlsplit(uri)
    path = Path(url2pathname((f"//{parsed.netloc}" if parsed.netloc else "") + parsed.path))
    if parsed.scheme != "file" or not path.is_absolute() or parsed.query or parsed.fragment:
        raise ValueError("配置资源不是当前目标机器的原生绝对 file URI")
    return path


class ExtensionConfigurationFiles:
    def __init__(self, initialization: ExtensionHostInitializationDTO, management: NodeExtensionManagement,
                 watcher: WorkspaceFileWatchService, stopping: asyncio.Event) -> None:
        resources = [initialization.environment.remote_settings]
        if initialization.workspace.configuration is not None:
            resources.append(initialization.workspace.configuration)
        self._view = None if initialization.workspace.configuration is None else NativeConfigurationView(
            id=initialization.workspace.id, native_uri=initialization.workspace.configuration.native_uri,
            folders=tuple(NativeConfigurationFolder(native_uri=folder.native_uri, name=folder.name)
                          for folder in initialization.workspace.folders))
        self._files = {resource.resource_uri: resource.native_uri for resource in resources}
        self._parents: dict[str, str] = {}
        self._changes: dict[str, str] = {}
        for resource in resources:
            parsed = parse_resource_uri(resource.resource_uri)
            parent = resource_display_uri(scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                tail_segments=parsed.tail_segments[:-1], gateway_authority=parsed.gateway_authority)
            native = _native_path(resource.native_uri)
            self._parents[parent] = native.parent.as_uri()
            self._changes[self._path_key(native)] = resource.resource_uri
        self._roots = tuple(dict.fromkeys(_native_path(value).parent for value in self._files.values()))
        self._management, self._watcher, self._stopping = management, watcher, stopping
        # watcher 按 filter 对象身份共享；同一代际的订阅必须复用这个对象。
        self._change_filter = self._accept_change

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _accept_change(self, change: WorkspaceFileChange) -> bool:
        return self._path_key(Path(change.path)) in self._changes

    def _native(self, resource_uri: str, *, parent: bool = False) -> str:
        if self._stopping.is_set():
            raise ValueError("配置资源所属宿主代际已停止")
        native = self._files.get(resource_uri)
        if native is None and parent:
            native = self._parents.get(resource_uri)
        if native is None:
            raise ValueError("配置资源不属于该宿主的明确 profile 或工作区视图")
        return native

    async def stat(self, resource_uri: str) -> ExtensionConfigurationStatDTO:
        result = await self._management.configuration_stat(self._native(resource_uri, parent=True), view=self._view)
        return ExtensionConfigurationStatDTO(resource_uri=resource_uri, **result.model_dump())

    async def read(self, resource_uri: str) -> ExtensionConfigurationReadDTO:
        result = await self._management.configuration_read(self._native(resource_uri), view=self._view)
        return ExtensionConfigurationReadDTO(resource_uri=resource_uri, **result.model_dump())

    async def write(self, resource_uri: str, content_base64: str,
                    options: ExtensionConfigurationWriteOptions) -> ExtensionConfigurationStatDTO:
        result = await self._management.configuration_write(self._native(resource_uri), content_base64, options, view=self._view)
        return ExtensionConfigurationStatDTO(resource_uri=resource_uri, **result.model_dump())

    async def events(self) -> AsyncIterator[ExtensionConfigurationEventDTO]:
        stream = self._watcher.subscribe_roots(self._roots, recursive=False, include_internal_paths=True,
                                              change_filter=self._change_filter)
        pending = asyncio.create_task(anext(stream))
        stopped = asyncio.create_task(self._stopping.wait())
        initialized = asyncio.create_task(self._watcher.wait_until_ready(
            self._roots, recursive=False, change_filter=self._change_filter,
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
                    kind=change.kind) for change in batch.changes]
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
