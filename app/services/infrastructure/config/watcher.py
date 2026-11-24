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
        self._directories = self._watch_roots(directories)
        self._candidate_paths = frozenset(path.resolve() for path in candidate_paths)
        self._on_change = on_change
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._rebind = asyncio.Event()

    def replace_candidates(self, *, directories: Iterable[Path], candidate_paths: Iterable[Path]) -> None:
        """同一监听器切换依赖集合；重绑后主动对账，覆盖换订阅窗口。"""
        roots = self._watch_roots(directories)
        candidates = frozenset(path.resolve() for path in candidate_paths)
        if set(roots) != set(self._directories) or candidates != self._candidate_paths:
            self._directories, self._candidate_paths = roots, candidates
            self._rebind.set()

    @staticmethod
    def _watch_roots(directories: Iterable[Path]) -> tuple[Path, ...]:
        roots = set()
        for directory in directories:
            root = directory.resolve()
            # 缺父目录也必须能观察其创建，不为监听创建配置目录。
            while not root.is_dir():
                if root == root.parent:
                    raise FileNotFoundError(f"配置监听目录没有现存祖先: {directory}")
                root = root.parent
            roots.add(root)
        return tuple(sorted(roots))

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
        while True:
            self._rebind.clear()
            first_iteration = True
            async for changes in awatch(
                *self._directories,
                debounce=200,
                step=50,
                rust_timeout=50,
                yield_on_timeout=True,
                stop_event=self._rebind,
                # 每个依赖只监听直接父目录；不递归进入会话运行时数据。
                recursive=False,
            ):
                if first_iteration or self._contains_candidate_change(changes):
                    await self._on_change()
                if first_iteration:
                    self._ready.set()
                first_iteration = False

    def _contains_candidate_change(
        self,
        changes: set[tuple[Change, str]],
    ) -> bool:
        return any(
            any(candidate.is_relative_to(Path(changed_path).resolve()) for candidate in self._candidate_paths)
            for _, changed_path in changes
        )
