"""按完整 watch key 共享的内置文件监视协调器。

共享 key 由 monitor instance + locator + recursive/filter/exclude/
correlation/options 组成：相同 key 的 consumer 共享同一条底层订阅并
按引用计数持有；最后一个引用释放时关闭底层订阅。consumer 只拿到可
释放的 FileMonitorHandle，绝不直接持有 watcher；溢出与错误都以显式
batch 暴露，不静默丢弃。

本模块不自己创建 OS watcher：底层监视经 FileWatchPort 注入，生产
装配复用 WorkspaceFileWatchService 的按 root 共享 watcher。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol

from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)

FILE_MONITOR_QUEUE_SIZE = 32
_WATCH_FILTERS = frozenset({"workspace_default", "resource_internal"})


@dataclass(frozen=True, slots=True)
class FileMonitorChange:
    kind: str
    path: str


@dataclass(frozen=True, slots=True)
class FileMonitorBatch:
    changes: tuple[FileMonitorChange, ...] = ()
    overflow: bool = False
    error: str | None = None


_CLOSED_MARKER = FileMonitorBatch(error="__file_monitor_closed__")


@dataclass(frozen=True, slots=True)
class FileMonitorKey:
    """完整 watch 语义 key；任何字段不同都不允许共享底层订阅。"""

    locator: str
    correlation: str
    recursive: bool = True
    filter_name: str = "workspace_default"
    exclude: tuple[str, ...] = ()
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        from pathlib import Path

        if not self.locator or not Path(self.locator).is_absolute():
            raise ValueError(f"文件监视 locator 必须是绝对路径: {self.locator}")
        if not self.correlation.strip():
            raise ValueError("文件监视 correlation 必须是非空用途标签")
        if self.filter_name not in _WATCH_FILTERS:
            raise ValueError(f"未知文件监视 filter: {self.filter_name}")
        if tuple(sorted(set(self.exclude))) != self.exclude:
            raise ValueError("文件监视 exclude 必须去重并按字典序排序")
        if tuple(sorted(set(self.options))) != self.options:
            raise ValueError("文件监视 options 必须去重并按字典序排序")


class FileWatchPort(Protocol):
    """底层文件监视端口；实现方负责按 key 的 watch 语义启动监视。"""

    def watch(self, key: FileMonitorKey) -> AsyncIterator[FileMonitorBatch]: ...


class WorkspaceFileWatchPort:
    """把 WorkspaceFileWatchService 适配为 FileWatchPort 的内置实现。

    服务本身只支持递归整树监视与固定 filter；recursive=False、自定义
    exclude 或未知 options 一律显式拒绝，绝不悄悄降级语义。
    """

    def __init__(self, service: WorkspaceFileWatchService) -> None:
        self._service = service

    async def watch(self, key: FileMonitorKey) -> AsyncIterator[FileMonitorBatch]:
        if not key.recursive:
            raise ValueError("WorkspaceFileWatchPort 只支持递归监视: recursive=False")
        if key.exclude:
            raise ValueError("WorkspaceFileWatchPort 不支持自定义 exclude")
        if key.options:
            raise ValueError(f"WorkspaceFileWatchPort 不支持额外 options: {key.options}")
        async for batch in self._service.subscribe_roots(
            (key.locator,),
            include_internal_paths=key.filter_name == "resource_internal",
        ):
            yield _map_batch(batch)


def _map_batch(batch: WorkspaceFileChangeBatch) -> FileMonitorBatch:
    return FileMonitorBatch(
        changes=tuple(
            FileMonitorChange(kind=change.kind, path=change.path)
            for change in batch.changes
        ),
        overflow=batch.overflow,
        error=batch.error,
    )


@dataclass(slots=True)
class _Share:
    key: FileMonitorKey
    pump_task: asyncio.Task[None]
    consumers: dict[asyncio.Queue[FileMonitorBatch], None] = field(default_factory=dict)


class FileMonitorHandle:
    """一个 consumer 的可释放订阅句柄；释放最后一个引用会关闭底层订阅。"""

    def __init__(self, *, monitor: SharedFileMonitor, key: FileMonitorKey, queue: asyncio.Queue[FileMonitorBatch]) -> None:
        self._monitor = monitor
        self._key = key
        self._queue = queue
        self._released = False

    @property
    def key(self) -> FileMonitorKey:
        return self._key

    @property
    def released(self) -> bool:
        return self._released

    async def batches(self) -> AsyncIterator[FileMonitorBatch]:
        """按 consumer 队列顺序读取 batch；句柄释放后迭代自然结束。"""
        while True:
            batch = await self._queue.get()
            if batch is _CLOSED_MARKER:
                return
            yield batch

    async def release(self) -> None:
        """释放本 consumer 的引用；重复释放幂等。"""
        if self._released:
            return
        self._released = True
        await self._monitor._release_consumer(self._key, self._queue)


class SharedFileMonitor:
    """进程内共享文件监视的唯一协调面；monitor instance 参与共享 key。"""

    def __init__(self, *, port: FileWatchPort, instance_id: str) -> None:
        if not instance_id.strip():
            raise ValueError("SharedFileMonitor instance_id 必须是非空字符串")
        self._port = port
        self._instance_id = instance_id
        self._shares: dict[FileMonitorKey, _Share] = {}
        self._closed = False

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def closed(self) -> bool:
        return self._closed

    async def subscribe(self, key: FileMonitorKey) -> FileMonitorHandle:
        """按完整 key 取得共享订阅；返回可释放 consumer handle。"""
        if self._closed:
            raise RuntimeError("SharedFileMonitor 已关闭，不允许新订阅")
        share = self._shares.get(key)
        if share is None:
            share = _Share(
                key=key,
                pump_task=asyncio.create_task(
                    self._pump(key),
                    name=f"boxteam-file-monitor:{self._instance_id}:{key.locator}",
                ),
            )
            self._shares[key] = share
        consumer_queue: asyncio.Queue[FileMonitorBatch] = asyncio.Queue(
            maxsize=FILE_MONITOR_QUEUE_SIZE,
        )
        share.consumers[consumer_queue] = None
        return FileMonitorHandle(monitor=self, key=key, queue=consumer_queue)

    async def close(self) -> None:
        """关闭全部共享订阅；重复 close 幂等。"""
        if self._closed:
            return
        self._closed = True
        shares = tuple(self._shares.values())
        self._shares.clear()
        errors: list[Exception] = []
        for share in shares:
            try:
                await self._stop_share(share)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if errors:
            raise ExceptionGroup(f"SharedFileMonitor close 失败: {self._instance_id}", errors)

    async def _release_consumer(self, key: FileMonitorKey, queue: asyncio.Queue[FileMonitorBatch]) -> None:
        share = self._shares.get(key)
        if share is None:
            return
        share.consumers.pop(queue, None)
        if not share.consumers:
            self._shares.pop(key, None)
            await self._stop_share(share)

    async def _stop_share(self, share: _Share) -> None:
        for queue in tuple(share.consumers):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(_CLOSED_MARKER)
            share.consumers.pop(queue, None)
        share.pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await share.pump_task

    async def _pump(self, key: FileMonitorKey) -> None:
        share = self._shares.get(key)
        if share is None:
            return
        iterator = self._port.watch(key)
        try:
            async for batch in iterator:
                self._fan_out(share, batch)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self._fan_out(share, FileMonitorBatch(error=f"文件监视失败: {key.locator}: {error}"))
            self._shares.pop(key, None)
        finally:
            with suppress(Exception):
                await iterator.aclose()

    def _fan_out(self, share: _Share, batch: FileMonitorBatch) -> None:
        for queue in tuple(share.consumers):
            try:
                queue.put_nowait(batch)
            except asyncio.QueueFull:
                # 溢出必须可见：丢掉最旧一条，显式投递 overflow 标记。
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(FileMonitorBatch(overflow=True))


__all__ = [
    "FileMonitorBatch",
    "FileMonitorChange",
    "FileMonitorHandle",
    "FileMonitorKey",
    "FileWatchPort",
    "SharedFileMonitor",
    "WorkspaceFileWatchPort",
]
