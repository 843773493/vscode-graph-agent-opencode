"""Terminal 原监控失败必须交回 Backend，取消观察不能遗弃原关闭。"""

from __future__ import annotations

import asyncio

import pytest

from app import main
from app.core.path_utils import workspace_context
from app.core.shielded_cleanup import run_cleanup_shielded
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.services.infrastructure.workspace_state_store import WorkspaceActivityService
from tests.integration.backend.sessions.test_attachment_maintenance import (
    maintenance_case as maintenance_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414
)


@pytest.fixture
def terminal_snapshot_fault(
    maintenance_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event, list[IsADirectoryError], list[str]]:
    workspace_id, root = list(maintenance_case.roots.items())[-1]
    obstruction = root / "blocked-terminal-snapshot"
    obstruction.mkdir()
    entered, release = asyncio.Event(), asyncio.Event()
    errors: list[IsADirectoryError] = []
    calls: list[str] = []

    async def settle() -> None:
        await release.wait()
        try:
            obstruction.read_text(encoding="utf-8")
        except IsADirectoryError as error:
            errors.append(error)
            raise

    async def terminal_snapshot(client: TerminalManagerClient) -> list[dict[str, object]]:
        if client._workspace_id == workspace_id:
            calls.append(workspace_id)
            entered.set()
            # 只替换外设传输：原监控、owner恢复、关闭链与存储仍走产品实现。
            await run_cleanup_shielded(settle)
        return []

    async def browser_snapshot(_client: BrowserManagerClient) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(TerminalManagerClient, "list_resources", terminal_snapshot)
    monkeypatch.setattr(BrowserManagerClient, "list_resources", browser_snapshot)
    return entered, release, errors, calls


@pytest.mark.parametrize("failed_before_close", [False, True], ids=["during-close", "already-failed"])
async def test_terminal_monitor_failure_blocks_dependencies_then_retries_same_workspace(
    maintenance_case: MountedCase,
    terminal_snapshot_fault: tuple[asyncio.Event, asyncio.Event, list[IsADirectoryError], list[str]],
    failed_before_close: bool,
) -> None:
    entered, release, errors, calls = terminal_snapshot_fault
    lifetime = main.lifespan(main.app)
    await lifetime.__aenter__()
    closing = observer = None
    try:
        peer, selected = maintenance_case.containers
        backend = main.app.state.workspace_lifespan
        workspace = next(item for item in backend.workspaces if item.container is selected)
        peer_lifetime = next(item for item in backend.workspaces if item.container is peer)
        workspace_owner = workspace._owner
        steering = selected.terminal_steering_service
        monitor = steering._task
        assert monitor is not None
        await asyncio.wait_for(entered.wait(), 3)
        with workspace_context(selected.workspace_id, selected.workspace_root,
                               session_catalog=selected.session_catalog):
            session = await selected.session_service.create(SessionCreateRequest(title="终端监控关闭"))
        record = await selected.workspace_activity_service.append(
            event_id="terminal-monitor-close", session_id=session.session_id,
            status="completed", summary="原监控失败仍保留活动事实",
        )
        locator = selected.session_catalog.resolver.resolve_session_node(session.session_id)
        if failed_before_close:
            release.set()
            done, _pending = await asyncio.wait((monitor,), timeout=3)
            assert done == {monitor}
        closing = asyncio.create_task(backend.close())
        if not failed_before_close:
            async with asyncio.timeout(3):
                while workspace.pending_close_stage != "terminal_steering.shutdown":
                    await asyncio.sleep(0)
            observer = asyncio.create_task(steering.shutdown())
            await asyncio.sleep(0)
            observer.cancel("取消终端关闭观察")
            done, _pending = await asyncio.wait((observer,), timeout=3)
            assert done == {observer}, "取消关闭观察不应等待原监控结算"
            with pytest.raises(asyncio.CancelledError, match="取消终端关闭观察"):
                await observer
            assert steering._task is monitor and not monitor.done() and monitor.cancelling() == 1
            release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(asyncio.shield(closing), 5)
        failure, = errors
        assert caught.value.exceptions == (failure,) and monitor.exception() is failure
        assert workspace.pending_close_stage == "terminal_steering.shutdown" and not workspace.closed
        assert steering._task is None and calls == [selected.workspace_id]
        assert peer_lifetime.closed and workspace._owner is workspace_owner
        assert main.app.state.workspace_container_registry is backend.registry and locator.is_dir()
        activity = selected.workspace_activity_service
        assert not activity.store._database._closed and record in activity.store.read_activity_page().records
        for container in maintenance_case.containers:
            with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
                container.workspace_root / ".boxteam/attachments", exclusive=True,
            ):
                pytest.fail("原监控失败尚未交回关闭方前不能释放维护锁")
        await backend.close()
        assert backend.closed and workspace.closed and workspace._owner is workspace_owner
        assert calls == [selected.workspace_id] and main.app.state.workspace_container_registry is None
        cold = WorkspaceActivityService(workspace_root=selected.workspace_root)
        try:
            assert record in cold.store.read_activity_page().records
        finally:
            cold.close()
        with AttachmentMaintenanceLock(selected.workspace_root / ".boxteam/attachments", exclusive=True):
            pass
    finally:
        release.set()
        await asyncio.gather(*(task for task in (closing, observer) if task is not None), return_exceptions=True)
        await lifetime.__aexit__(None, None, None)
