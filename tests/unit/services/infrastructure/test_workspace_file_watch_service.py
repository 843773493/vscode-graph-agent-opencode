from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.infrastructure.workspace_file_watch_service import (
    FILE_WATCH_QUEUE_SIZE,
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
