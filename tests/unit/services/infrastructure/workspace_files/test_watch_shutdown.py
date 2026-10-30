"""监听任务的原生清理仍在执行时，原 owner 不能丢弃或再次取消它。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from watchfiles import Change

import app.services.infrastructure.workspace_file_watch_service as watch_module
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)


@dataclass
class NativeWatch:
    initialized: asyncio.Event = field(default_factory=asyncio.Event)
    cleaning: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    starts: int = 0


@dataclass
class WatchCase:
    service: WorkspaceFileWatchService
    roots: tuple[Path, ...]
    native: NativeWatch
    queue: asyncio.Queue[WorkspaceFileChangeBatch]


@pytest.fixture
async def watch_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WatchCase]:
    root = tmp_path / "workspace"
    root.mkdir()
    native = NativeWatch()
    native.initialized.set()

    async def watch(_root: Path, **_options: object) -> AsyncIterator[set[tuple[Change, str]]]:
        native.starts += 1
        try:
            await native.initialized.wait()
            yield set()
            await asyncio.Event().wait()
        finally:
            native.cleaning.set()
            try:
                await native.release.wait()
            except asyncio.CancelledError:
                native.interrupted = True
                raise
            finally:
                native.finished.set()

    monkeypatch.setattr(watch_module, "awatch", watch)
    service = WorkspaceFileWatchService(workspace_root=root)
    queue: asyncio.Queue[WorkspaceFileChangeBatch] = asyncio.Queue()
    await service._acquire((root,), queue, include_internal_paths=False)
    try:
        yield WatchCase(service, (root,), native, queue)
    finally:
        native.release.set()
        await service.shutdown()


async def _turns() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("release_subscription", [False, True], ids=["shutdown", "last-subscriber"])
async def test_original_watcher_stays_registered_until_cleanup_finishes(
    watch_case: WatchCase, release_subscription: bool,
) -> None:
    service, native = watch_case.service, watch_case.native
    key = service._watcher_key(watch_case.roots[0], True, None)
    original = service._watchers[key].task
    close = asyncio.create_task(
        service._release(watch_case.roots, watch_case.queue) if release_subscription else service.shutdown()
    )
    try:
        await asyncio.wait_for(native.cleaning.wait(), 2)
        assert not original.done() and not close.done()
        assert service._watchers[key].task is original
    finally:
        native.release.set()
        await close
    assert native.finished.is_set() and not native.interrupted
    assert service._watchers == {}


@pytest.mark.parametrize("cancel_first", [False, True], ids=["overlap", "cancelled-waiter"])
async def test_repeated_shutdown_waits_for_original_cleanup_without_recancelling(
    watch_case: WatchCase, cancel_first: bool,
) -> None:
    service, native = watch_case.service, watch_case.native
    first = asyncio.create_task(service.shutdown())
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(native.cleaning.wait(), 2)
        if cancel_first:
            first.cancel("只取消关闭观察者")
            with pytest.raises(asyncio.CancelledError, match="只取消关闭观察者"):
                await first
        second = asyncio.create_task(service.shutdown())
        await _turns()
        assert not second.done(), "重试不能将尚未完成的原生清理当作已经关闭"
        assert not native.finished.is_set() and not native.interrupted
        assert native.starts == 1
    finally:
        native.release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
    assert service._watchers == {} and not native.interrupted


async def test_new_subscription_waits_for_last_subscriber_cleanup(watch_case: WatchCase) -> None:
    service, native = watch_case.service, watch_case.native
    close = asyncio.create_task(service._release(watch_case.roots, watch_case.queue))
    replacement_queue: asyncio.Queue[WorkspaceFileChangeBatch] = asyncio.Queue()
    replacement: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(native.cleaning.wait(), 2)
        replacement = asyncio.create_task(service._acquire(
            watch_case.roots, replacement_queue, include_internal_paths=False,
        ))
        await _turns()
        assert not replacement.done() and native.starts == 1
        native.release.set()
        await asyncio.wait_for(asyncio.gather(close, replacement), 2)
        assert native.starts == 2
        key = service._watcher_key(watch_case.roots[0], True, None)
        assert set(service._watchers[key].subscribers) == {replacement_queue}
    finally:
        native.release.set()
        await asyncio.gather(close, *([replacement] if replacement is not None else []), return_exceptions=True)


async def test_shutdown_rejects_new_subscriptions_and_ready_waits(watch_case: WatchCase) -> None:
    service, native = watch_case.service, watch_case.native
    close = asyncio.create_task(service.shutdown())
    try:
        await asyncio.wait_for(native.cleaning.wait(), 2)
        with pytest.raises(RuntimeError, match="文件监听服务.*停止.*订阅"):
            await service._acquire(watch_case.roots, asyncio.Queue(), include_internal_paths=False)
        with pytest.raises(RuntimeError, match="文件监听服务.*停止.*订阅"):
            await service.wait_until_ready(watch_case.roots)
        assert native.starts == 1
    finally:
        native.release.set()
        await close
    with pytest.raises(RuntimeError, match="文件监听服务.*停止.*订阅"):
        await service._acquire(watch_case.roots, asyncio.Queue(), include_internal_paths=False)


async def test_cancelling_subscription_before_ready_releases_its_registered_watcher(watch_case: WatchCase) -> None:
    service, native = watch_case.service, watch_case.native
    root = watch_case.roots[0].parent / "initializing"
    root.mkdir()
    native.initialized.clear()
    stream = service.subscribe_roots((root,))
    pending = asyncio.create_task(anext(stream))
    try:
        await _turns()
        assert native.starts == 2 and not pending.done()
        pending.cancel("取消未就绪订阅")
        await asyncio.wait_for(native.cleaning.wait(), 2)
        assert not pending.done(), "原生清理完成前仍由本次订阅收尾持有"
        native.release.set()
        with pytest.raises(asyncio.CancelledError, match="取消未就绪订阅"):
            await pending
        assert set(service._watchers) == {service._watcher_key(watch_case.roots[0], True, None)}
        assert service._watchers[next(iter(service._watchers))].subscribers == {watch_case.queue: False}
        assert not native.interrupted
    finally:
        native.release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


async def test_other_root_can_subscribe_while_original_root_is_cleaning(watch_case: WatchCase) -> None:
    service, native = watch_case.service, watch_case.native
    root = watch_case.roots[0].parent / "peer"
    root.mkdir()
    close = asyncio.create_task(service._release(watch_case.roots, watch_case.queue))
    try:
        await asyncio.wait_for(native.cleaning.wait(), 2)
        await asyncio.wait_for(service._acquire((root,), asyncio.Queue(), include_internal_paths=False), 2)
        assert native.starts == 2 and not close.done()
        assert not native.finished.is_set()
    finally:
        native.release.set()
        await close


@pytest.mark.parametrize("failure_count", [1, 2])
async def test_shutdown_keeps_original_cleanup_failures_visible_on_retry(
    watch_case: WatchCase, monkeypatch: pytest.MonkeyPatch, failure_count: int,
) -> None:
    root = watch_case.roots[0].parent / "failing-close"
    roots = tuple(root / str(index) for index in range(3))
    for directory in roots:
        directory.mkdir(parents=True)
    service = WorkspaceFileWatchService(workspace_root=root)
    failures = {directory: RuntimeError(f"原监听清理失败-{index}")
                for index, directory in enumerate(roots[:failure_count])}
    original = service._watch_root

    async def watch(
        directory: Path, subscribers: dict[asyncio.Queue[WorkspaceFileChangeBatch], bool],
        ready: asyncio.Event, *, recursive: bool, change_filter: watch_module.WorkspaceFileChangeFilter | None,
    ) -> None:
        try:
            await original(directory, subscribers, ready, recursive=recursive, change_filter=change_filter)
        except asyncio.CancelledError:
            if directory in failures:
                raise failures[directory] from None
            raise

    monkeypatch.setattr(service, "_watch_root", watch)
    watch_case.native.release.set()
    await service._acquire(roots, asyncio.Queue(), include_internal_paths=False)
    starts = watch_case.native.starts
    for _ in range(2):
        with pytest.raises((RuntimeError, BaseExceptionGroup)) as caught:
            await service.shutdown()
        errors = caught.value.exceptions if isinstance(caught.value, BaseExceptionGroup) else (caught.value,)
        assert errors == tuple(failures.values())
        assert set(service._watchers) == {service._watcher_key(directory, True, None) for directory in failures}
        assert all(watcher.task.done() for watcher in service._watchers.values())
        assert watch_case.native.starts == starts
