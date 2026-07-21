from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from watchfiles import Change, awatch


class ConfigFileWatcher:
    def __init__(
        self,
        *,
        directories: Iterable[Path],
        candidate_paths: Iterable[Path],
        on_change: Callable[[], Awaitable[None]],
    ) -> None:
        self._directories = tuple(dict.fromkeys(path.resolve() for path in directories))
        self._candidate_paths = frozenset(path.resolve() for path in candidate_paths)
        self._on_change = on_change
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("配置文件监听器不允许重复启动")
        for directory in self._directories:
            if not directory.is_dir():
                raise FileNotFoundError(f"配置监听目录不存在: {directory}")
        self._ready.clear()
        self._task = asyncio.create_task(
            self._watch_loop(),
            name="boxteam-config-watcher",
        )
        self._task.add_done_callback(lambda _task: self._ready.set())
        await self._ready.wait()
        if self._task.done():
            failed_task = self._task
            self._task = None
            failed_task.result()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _watch_loop(self) -> None:
        first_iteration = True
        async for changes in awatch(
            *self._directories,
            debounce=200,
            step=50,
            rust_timeout=50,
            yield_on_timeout=True,
        ):
            if first_iteration:
                self._ready.set()
                first_iteration = False
            if self._contains_candidate_change(changes):
                await self._on_change()

    def _contains_candidate_change(
        self,
        changes: set[tuple[Change, str]],
    ) -> bool:
        return any(
            Path(changed_path).resolve() in self._candidate_paths
            for _, changed_path in changes
        )
