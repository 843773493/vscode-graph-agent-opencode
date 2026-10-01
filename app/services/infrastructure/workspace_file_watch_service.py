from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from watchfiles import Change, awatch
from watchfiles.filters import DefaultFilter

logger = logging.getLogger(__name__)

FILE_WATCH_DEBOUNCE_MS = 200
FILE_WATCH_STEP_MS = 50
FILE_WATCH_QUEUE_SIZE = 32
WORKSPACE_FILE_WATCH_FILTER = DefaultFilter(
    ignore_dirs=[*DefaultFilter.ignore_dirs, ".boxteam"],
)
# 底层共享 watcher 必须能看到 ResourceRegistry 登记的内部资源；是否向
# 订阅者暴露由 `_publish` 按订阅选项决定。
_RESOURCE_FILE_WATCH_FILTER = DefaultFilter(
    ignore_dirs=list(DefaultFilter.ignore_dirs),
)


@dataclass(frozen=True, slots=True)
class WorkspaceFileChange:
    kind: Literal["create", "edit", "delete"]
    path: str


@dataclass(frozen=True, slots=True)
class WorkspaceFileChangeBatch:
    changes: tuple[WorkspaceFileChange, ...] = ()
    overflow: bool = False
    error: str | None = None


@dataclass(slots=True)
class _SharedWatcher:
    task: asyncio.Task[None]
    subscribers: dict[asyncio.Queue[WorkspaceFileChangeBatch], bool]
    ready: asyncio.Event


class WorkspaceFileWatchService:
    """按路径复用底层 watcher，并把变更批量分发给 SSE 订阅者。"""

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._watchers: dict[Path, _SharedWatcher] = {}
        self._lock = asyncio.Lock()

    def resolve_watch_roots(self, extra_paths: Iterable[str]) -> tuple[Path, ...]:
        candidates = [self._workspace_root]
        for value in extra_paths:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                raise ValueError(f"文件监听路径必须是绝对目录: {value}")
            candidates.append(candidate.resolve())
        unique = sorted(set(candidates), key=lambda path: (len(path.parts), str(path)))
        roots: list[Path] = []
        for path in unique:
            if path == Path(path.anchor):
                raise ValueError(f"禁止递归监听文件系统根目录: {path}")
            if not path.exists():
                raise FileNotFoundError(f"文件监听路径不存在: {path}")
            if not path.is_dir():
                raise NotADirectoryError(f"文件监听路径不是目录: {path}")
            if any(path == root or path.is_relative_to(root) for root in roots):
                continue
            roots.append(path)
        return tuple(roots)

    async def subscribe(
        self,
        extra_paths: Iterable[str],
        *,
        include_internal_paths: bool = False,
    ) -> AsyncIterator[WorkspaceFileChangeBatch]:
        async for batch in self.subscribe_roots(
            self.resolve_watch_roots(extra_paths),
            include_internal_paths=include_internal_paths,
        ):
            yield batch

    async def subscribe_roots(
        self,
        roots: tuple[Path, ...],
        *,
        include_internal_paths: bool = False,
    ) -> AsyncIterator[WorkspaceFileChangeBatch]:
        queue: asyncio.Queue[WorkspaceFileChangeBatch] = asyncio.Queue(
            maxsize=FILE_WATCH_QUEUE_SIZE,
        )
        await self._acquire(
            roots,
            queue,
            include_internal_paths=include_internal_paths,
        )
        try:
            while True:
                yield await queue.get()
        finally:
            await self._release(roots, queue)

    async def wait_until_ready(self, roots: tuple[Path, ...]) -> None:
        """等待指定共享 watcher 已完成首次底层监听初始化。"""
        resolved_roots = tuple(path.resolve() for path in roots)
        for root in resolved_roots:
            while True:
                async with self._lock:
                    watcher = self._watchers.get(root)
                if watcher is None:
                    await asyncio.sleep(0)
                    continue
                await watcher.ready.wait()
                if watcher.task.done():
                    watcher.task.result()
                break

    async def shutdown(self) -> None:
        async with self._lock:
            watchers = tuple(self._watchers.values())
            self._watchers.clear()
        for watcher in watchers:
            self._publish(
                watcher.subscribers,
                WorkspaceFileChangeBatch(error="文件监听服务已停止"),
            )
            watcher.task.cancel()
        for watcher in watchers:
            with suppress(asyncio.CancelledError):
                await watcher.task

    async def _acquire(
        self,
        roots: tuple[Path, ...],
        queue: asyncio.Queue[WorkspaceFileChangeBatch],
        *,
        include_internal_paths: bool,
    ) -> None:
        watchers_to_ready: list[_SharedWatcher] = []
        async with self._lock:
            for root in roots:
                watcher = self._watchers.get(root)
                if watcher is None or watcher.task.done():
                    if watcher is not None:
                        watcher.task.result()
                    subscribers = {queue: include_internal_paths}
                    ready = asyncio.Event()
                    task = asyncio.create_task(
                        self._watch_root(root, subscribers, ready),
                        name=f"boxteam-file-watch:{root}",
                    )
                    watcher = _SharedWatcher(
                        task=task,
                        subscribers=subscribers,
                        ready=ready,
                    )
                    self._watchers[root] = watcher
                else:
                    watcher.subscribers[queue] = include_internal_paths
                watchers_to_ready.append(watcher)
        for watcher in watchers_to_ready:
            await watcher.ready.wait()
            if watcher.task.done():
                watcher.task.result()

    async def _release(
        self,
        roots: tuple[Path, ...],
        queue: asyncio.Queue[WorkspaceFileChangeBatch],
    ) -> None:
        tasks_to_stop: list[asyncio.Task[None]] = []
        async with self._lock:
            for root in roots:
                watcher = self._watchers.get(root)
                if watcher is None:
                    continue
                watcher.subscribers.pop(queue, None)
                if watcher.subscribers:
                    continue
                if self._watchers.get(root) is watcher:
                    del self._watchers[root]
                if not watcher.task.done():
                    watcher.task.cancel()
                tasks_to_stop.append(watcher.task)
        for task in tasks_to_stop:
            with suppress(asyncio.CancelledError):
                await task

    async def _watch_root(
        self,
        root: Path,
        subscribers: dict[asyncio.Queue[WorkspaceFileChangeBatch], bool],
        ready: asyncio.Event,
    ) -> None:
        logger.info("开始共享文件监听: root=%s", root)
        try:
            first_iteration = True
            async for raw_changes in awatch(
                root,
                # 先接收内部资源，再按订阅者过滤；否则 ResourceRegistry
                # 永远收不到 `.boxteam/skills` 的变化。
                watch_filter=_RESOURCE_FILE_WATCH_FILTER,
                debounce=FILE_WATCH_DEBOUNCE_MS,
                step=FILE_WATCH_STEP_MS,
                rust_timeout=FILE_WATCH_STEP_MS,
                yield_on_timeout=True,
            ):
                if first_iteration:
                    ready.set()
                    first_iteration = False
                changes = tuple(
                    WorkspaceFileChange(
                        kind=self._change_kind(change),
                        path=str(Path(path).resolve()),
                    )
                    for change, path in sorted(raw_changes, key=lambda item: item[1])
                )
                if changes:
                    self._publish(
                        subscribers,
                        WorkspaceFileChangeBatch(changes=changes),
                        internal_root=self._workspace_root / ".boxteam",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("共享文件监听失败: root=%s", root)
            self._publish(
                subscribers,
                WorkspaceFileChangeBatch(error=f"文件监听失败: {root}: {error}"),
            )
        finally:
            ready.set()
            logger.info("停止共享文件监听: root=%s", root)

    @staticmethod
    def _publish(
        subscribers: (
            Mapping[asyncio.Queue[WorkspaceFileChangeBatch], bool]
            | set[asyncio.Queue[WorkspaceFileChangeBatch]]
        ),
        batch: WorkspaceFileChangeBatch,
        *,
        internal_root: Path | None = None,
    ) -> None:
        if isinstance(subscribers, set):
            subscriber_items = tuple((queue, True) for queue in subscribers)
        else:
            subscriber_items = tuple(subscribers.items())
        for queue, include_internal_paths in subscriber_items:
            if (
                internal_root is not None
                and not include_internal_paths
                and batch.error is None
                and not batch.overflow
            ):
                changes = tuple(
                    change
                    for change in batch.changes
                    if not _is_path_under(change.path, internal_root)
                )
                if not changes:
                    continue
                batch_for_subscriber = WorkspaceFileChangeBatch(
                    changes=changes,
                )
            else:
                batch_for_subscriber = batch
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    batch_for_subscriber
                    if batch_for_subscriber.error is not None
                    else WorkspaceFileChangeBatch(overflow=True)
                )
                continue
            queue.put_nowait(batch_for_subscriber)

    @staticmethod
    def _change_kind(change: Change) -> Literal["create", "edit", "delete"]:
        if change == Change.added:
            return "create"
        if change == Change.deleted:
            return "delete"
        if change == Change.modified:
            return "edit"
        raise ValueError(f"未知文件变更类型: {change}")

def _is_path_under(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
