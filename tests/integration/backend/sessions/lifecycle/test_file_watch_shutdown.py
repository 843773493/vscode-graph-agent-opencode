"""真实监听退出后的收尾挂起时，Backend仍持有原scope并沿同一任务排空。"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from app import main
from app.backend_lifecycle import BackendLifespan
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
    WorkspaceFileChangeFilter,
    WorkspaceFileWatchService,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414
)


async def test_backend_keeps_original_watcher_during_close_timeout_and_retry(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_root = list(mounted_case.roots.values())[-1]
    entered, release = asyncio.Event(), asyncio.Event()
    interrupted: list[asyncio.CancelledError] = []
    original_watch = WorkspaceFileWatchService._watch_root

    async def watch(
        service: WorkspaceFileWatchService, root: Path,
        subscribers: dict[asyncio.Queue[WorkspaceFileChangeBatch], bool], ready: asyncio.Event,
        *, recursive: bool, change_filter: WorkspaceFileChangeFilter | None,
    ) -> None:
        try:
            await original_watch(service, root, subscribers, ready,
                                 recursive=recursive, change_filter=change_filter)
        finally:
            # 实际watchfiles先完成原生退出；这里只暂停原任务提交最终完成的边界。
            if service._workspace_root == blocked_root:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError as error:
                    interrupted.append(error)
                    raise

    monkeypatch.setattr(WorkspaceFileWatchService, "_watch_root", watch)
    deadlines: list[asyncio.Timeout] = []

    def deadline(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        value = asyncio.timeout(seconds)
        deadlines.append(value)
        return value

    monkeypatch.setattr(inspect.getmodule(BackendLifespan), "timeout", deadline)
    lifetime = main.lifespan(main.app)
    _main_lifespan_state = await lifetime.__aenter__()
    await _main_lifespan_state["after_http_startup"]()
    waiting: asyncio.Task[None] | None = None
    watcher_wait: asyncio.Task[None] | None = None
    try:
        peer, selected = mounted_case.containers
        service = selected.resource_platform.file_watch_service
        original = dict(service._watchers)
        assert original, "真实启动必须已通过资源平台建立文件监听"
        backend: BackendLifespan = main.app.state.workspace_lifespan
        registry = backend.registry
        selected_lifetime = next(item for item in backend.workspaces if item.container is selected)
        peer_lifetime = next(item for item in backend.workspaces if item.container is peer)
        waiting = asyncio.create_task(backend.close())
        await asyncio.wait_for(entered.wait(), 5)
        assert peer_lifetime._owner is not None
        await asyncio.wait_for(asyncio.shield(peer_lifetime._owner), 5)
        assert peer_lifetime.closed and selected_lifetime.pending_close_stage == "resource_platform.close"
        assert not selected.resource_platform.process_root_scope.is_closed
        pending = {key: value for key, value in original.items() if not value.task.done()}
        assert pending and all(service._watchers[key] is value for key, value in pending.items())
        watcher_wait = asyncio.create_task(service.shutdown())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not watcher_wait.done()
        watcher_wait.cancel("取消监听关闭观察者")
        with pytest.raises(asyncio.CancelledError, match="取消监听关闭观察者"):
            await watcher_wait
        assert not interrupted and all(not value.task.done() for value in pending.values())
        owner = backend._close_owner
        assert owner is not None and len(deadlines) == 1
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError, match="stage=workspaces.*resource_platform.close"):
            await waiting
        assert backend._close_owner is owner and not owner.done()
        assert main.app.state.workspace_container_registry is registry
        with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
            selected.workspace_root / ".boxteam/attachments", exclusive=True,
        ):
            pytest.fail("监听原任务未完成前不能归还维护锁")
        release.set()
        await asyncio.wait_for(backend.close(), 5)
        await service.shutdown()
        assert service._watchers == {} and all(value.task.done() for value in original.values())
        assert backend._close_owner is owner and backend.closed and not interrupted
        assert selected_lifetime.closed and selected.resource_platform.process_root_scope.is_closed
        assert main.app.state.workspace_container_registry is None
        with AttachmentMaintenanceLock(selected.workspace_root / ".boxteam/attachments", exclusive=True):
            pass
    finally:
        release.set()
        await asyncio.gather(*(task for task in (waiting, watcher_wait) if task is not None), return_exceptions=True)
        await lifetime.__aexit__(None, None, None)
