from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from watchfiles import Change

from app.services.infrastructure.workspace_file_watch_service import (
    FILE_WATCH_QUEUE_SIZE,
    WORKSPACE_FILE_WATCH_FILTER,
    WorkspaceFileChange,
    WorkspaceFileChangeBatch,
    WorkspaceFileWatchService,
)


def test_watch_roots_deduplicate_nested_shortcuts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    external = tmp_path / "external"
    nested.mkdir(parents=True)
    external.mkdir()
    service = WorkspaceFileWatchService(workspace_root=workspace)

    roots = service.resolve_watch_roots([str(nested), str(external), str(external)])

    assert roots == (external.resolve(), workspace.resolve())


def test_watch_roots_reject_filesystem_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = WorkspaceFileWatchService(workspace_root=workspace)

    with pytest.raises(ValueError, match="禁止递归监听文件系统根目录"):
        service.resolve_watch_roots([str(Path(tmp_path.anchor))])

    with pytest.raises(ValueError, match="必须是绝对目录"):
        service.resolve_watch_roots(["relative/path"])


def test_watcher_filters_boxteam_runtime_before_watch_batches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    internal_file = workspace / ".boxteam" / "sessions" / "stream.state.json"
    source_file = workspace / "parry_arena" / "main.gd"

    assert WORKSPACE_FILE_WATCH_FILTER(Change.modified, str(internal_file)) is False
    assert WORKSPACE_FILE_WATCH_FILTER(Change.modified, str(source_file)) is True


def test_queue_overflow_is_reported_instead_of_silently_dropping() -> None:
    queue: asyncio.Queue[WorkspaceFileChangeBatch] = asyncio.Queue(
        maxsize=FILE_WATCH_QUEUE_SIZE,
    )
    subscribers = {queue}
    batch = WorkspaceFileChangeBatch(
        changes=(WorkspaceFileChange(kind="edit", path="/tmp/example"),),
    )
    for _ in range(FILE_WATCH_QUEUE_SIZE):
        WorkspaceFileWatchService._publish(subscribers, batch)

    WorkspaceFileWatchService._publish(subscribers, batch)

    assert queue.qsize() == 1
    assert queue.get_nowait().overflow is True

    for _ in range(FILE_WATCH_QUEUE_SIZE):
        WorkspaceFileWatchService._publish(subscribers, batch)
    WorkspaceFileWatchService._publish(
        subscribers,
        WorkspaceFileChangeBatch(error="watch stopped"),
    )
    assert queue.qsize() == 1
    assert queue.get_nowait().error == "watch stopped"


@pytest.mark.asyncio
async def test_live_watcher_delivers_external_file_change(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = WorkspaceFileWatchService(workspace_root=workspace)
    stream = service.subscribe([])
    try:
        next_batch = asyncio.create_task(anext(stream))
        while not service._watchers or not all(
            watcher.ready.is_set() for watcher in service._watchers.values()
        ):
            await asyncio.sleep(0)

        changed_file = workspace / "external.txt"
        changed_file.write_text("changed", encoding="utf-8")
        batch = await asyncio.wait_for(next_batch, timeout=2)

        assert any(
            change.kind == "create" and change.path == str(changed_file.resolve())
            for change in batch.changes
        )
    finally:
        await stream.aclose()
        await service.shutdown()


@pytest.mark.asyncio
async def test_live_watcher_ignores_workspace_internal_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    internal = workspace / ".boxteam"
    internal.mkdir(parents=True)
    service = WorkspaceFileWatchService(workspace_root=workspace)
    stream = service.subscribe([])
    try:
        first_batch = asyncio.create_task(anext(stream))
        while not service._watchers or not all(
            watcher.ready.is_set() for watcher in service._watchers.values()
        ):
            await asyncio.sleep(0)
        internal_file = internal / "rollout" / "index.sqlite-shm"
        internal_file.parent.mkdir()
        internal_file.write_bytes(b"internal")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(first_batch, timeout=0.5)
    finally:
        await stream.aclose()
        await service.shutdown()


def _watch_queue() -> asyncio.Queue[WorkspaceFileChangeBatch]:
    return asyncio.Queue(maxsize=FILE_WATCH_QUEUE_SIZE)


@pytest.mark.asyncio
async def test_same_root_subscribers_share_single_watcher_task(
    tmp_path: Path,
) -> None:
    """同一 root 的多个订阅者必须共享同一条底层 watcher 任务。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = WorkspaceFileWatchService(workspace_root=workspace)
    roots = service.resolve_watch_roots([])
    queue_a = _watch_queue()
    queue_b = _watch_queue()
    try:
        await service._acquire(roots, queue_a, include_internal_paths=False)
        await service.wait_until_ready(roots)
        task_a = service._watchers[roots[0]].task

        await service._acquire(roots, queue_b, include_internal_paths=False)

        assert len(service._watchers) == 1
        assert service._watchers[roots[0]].task is task_a
        assert len(service._watchers[roots[0]].subscribers) == 2
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_last_subscriber_release_stops_underlying_watcher(
    tmp_path: Path,
) -> None:
    """非末位释放保留底层 watcher；末位释放才停止并移除它。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = WorkspaceFileWatchService(workspace_root=workspace)
    roots = service.resolve_watch_roots([])
    queue_a = _watch_queue()
    queue_b = _watch_queue()
    try:
        await service._acquire(roots, queue_a, include_internal_paths=False)
        await service.wait_until_ready(roots)
        await service._acquire(roots, queue_b, include_internal_paths=False)
        task_a = service._watchers[roots[0]].task

        await service._release(roots, queue_b)
        # 仍有订阅者：底层 watcher 必须存活。
        assert len(service._watchers) == 1
        assert task_a.done() is False

        await service._release(roots, queue_a)
        # 末位释放：底层 watcher 停止并移除。
        assert service._watchers == {}
        assert task_a.done() is True
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_different_roots_do_not_share_watcher(tmp_path: Path) -> None:
    """不同 root 各持有独立 watcher 任务，不互相共享。"""
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    service = WorkspaceFileWatchService(workspace_root=workspace)
    workspace_roots = service.resolve_watch_roots([])
    external_roots = service.resolve_watch_roots([str(external)])
    try:
        await service._acquire(
            workspace_roots, _watch_queue(), include_internal_paths=False
        )
        await service.wait_until_ready(workspace_roots)
        await service._acquire(
            external_roots, _watch_queue(), include_internal_paths=False
        )
        await service.wait_until_ready(external_roots)

        assert len(service._watchers) == 2
        assert (
            service._watchers[workspace_roots[0]].task
            is not service._watchers[external_roots[0]].task
        )
    finally:
        await service.shutdown()
