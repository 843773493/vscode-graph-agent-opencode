from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
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
    subscribers: set[asyncio.Queue[WorkspaceFileChangeBatch]]
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
    ) -> AsyncIterator[WorkspaceFileChangeBatch]:
        async for batch in self.subscribe_roots(
            self.resolve_watch_roots(extra_paths),
        ):
            yield batch

    async def subscribe_roots(
        self,
        roots: tuple[Path, ...],
    ) -> AsyncIterator[WorkspaceFileChangeBatch]:
        queue: asyncio.Queue[WorkspaceFileChangeBatch] = asyncio.Queue(
            maxsize=FILE_WATCH_QUEUE_SIZE,
        )
        await self._acquire(roots, queue)
        try:
            while True:
                yield await queue.get()
        finally:
            await self._release(roots, queue)

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
    ) -> None:
        watchers_to_ready: list[_SharedWatcher] = []
        async with self._lock:
            for root in roots:
                watcher = self._watchers.get(root)
                if watcher is None or watcher.task.done():
                    if watcher is not None:
                        watcher.task.result()
                    subscribers = {queue}
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
                    watcher.subscribers.add(queue)
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
                watcher.subscribers.discard(queue)
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
        subscribers: set[asyncio.Queue[WorkspaceFileChangeBatch]],
        ready: asyncio.Event,
    ) -> None:
        logger.info("开始共享文件监听: root=%s", root)
        try:
            first_iteration = True
            async for raw_changes in awatch(
                root,
                watch_filter=WORKSPACE_FILE_WATCH_FILTER,
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
                    if not self._is_internal_workspace_path(Path(path))
                )
                if changes:
                    self._publish(subscribers, WorkspaceFileChangeBatch(changes=changes))
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
        subscribers: set[asyncio.Queue[WorkspaceFileChangeBatch]],
        batch: WorkspaceFileChangeBatch,
    ) -> None:
        for queue in tuple(subscribers):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    batch
                    if batch.error is not None
                    else WorkspaceFileChangeBatch(overflow=True)
                )
                continue
            queue.put_nowait(batch)

    @staticmethod
    def _change_kind(change: Change) -> Literal["create", "edit", "delete"]:
        if change == Change.added:
            return "create"
        if change == Change.deleted:
            return "delete"
        if change == Change.modified:
            return "edit"
        raise ValueError(f"未知文件变更类型: {change}")

    def _is_internal_workspace_path(self, path: Path) -> bool:
        """过滤工作区内部状态，避免会话状态刷新文件树。"""
        internal_root = self._workspace_root / ".boxteam"
        try:
            path.resolve().relative_to(internal_root)
        except ValueError:
            return False
        return True
