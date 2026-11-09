"""驻留原扫描关闭期间，真实双Workspace保留依赖并交付迟到结果。"""

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
from app.services.orchestration.thread_residency import ThreadResidencyTracker
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
def residency_scan_boundary(
    maintenance_case: MountedCase, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> tuple[asyncio.Event, asyncio.Event, list[Exception], list[int]]:
    workspace_id, root = list(maintenance_case.roots.items())[-1]
    obstruction = root / "blocked-residency-receipt"
    obstruction.mkdir()
    entered, finish = asyncio.Event(), asyncio.Event()
    failures: list[Exception] = []
    calls: list[int] = []
    original_sweep, original_sleep = ThreadResidencyTracker.sweep, asyncio.sleep

    async def settle() -> None:
        await finish.wait()
        if outcome != "success":
            # 实际文件系统失败由原扫描回执交回，不用构造错误替代该边界。
            try:
                obstruction.read_text(encoding="utf-8")
            except IsADirectoryError as error:
                failures[:] = [error]
                raise

    async def sweep(tracker: ThreadResidencyTracker) -> None:
        await original_sweep(tracker)
        if any(container.workspace_id == workspace_id
               and container.agent_execution_service._residency_tracker is tracker
               for container in maintenance_case.containers):
            calls.append(len(calls) + 1)
            if len(calls) == 2:
                entered.set()
                await run_cleanup_shielded(settle)

    async def sleep(delay: float) -> None:
        if delay == 60 and any(
            container.workspace_id == workspace_id
            and container.agent_execution_service._sweep_task is asyncio.current_task()
            for container in maintenance_case.containers
        ):
            await original_sleep(0)
        else:
            await original_sleep(delay)

    async def empty_snapshot(_client: TerminalManagerClient | BrowserManagerClient) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(ThreadResidencyTracker, "sweep", sweep)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    # 外设传输不访问共享debug；扫描、原关闭owner、Session及存储仍用真实实现。
    monkeypatch.setattr(TerminalManagerClient, "list_resources", empty_snapshot)
    monkeypatch.setattr(BrowserManagerClient, "list_resources", empty_snapshot)
    return entered, finish, failures, calls


@pytest.mark.parametrize("outcome", ["success", "late-error", "default-deadline"])
async def test_residency_shutdown_keeps_owner_and_dependencies_until_original_scan_settles(
    maintenance_case: MountedCase,
    residency_scan_boundary: tuple[asyncio.Event, asyncio.Event, list[Exception], list[int]],
    outcome: str,
) -> None:
    entered, finish, failures, calls = residency_scan_boundary
    lifetime = main.lifespan(main.app)
    _main_lifespan_state = await lifetime.__aenter__()
    await _main_lifespan_state["after_http_startup"]()
    closing = observer = None
    selected = maintenance_case.containers[-1]
    service = selected.agent_execution_service
    worker = service._sweep_task
    assert worker is not None
    try:
        backend = main.app.state.workspace_lifespan
        workspace = next(item for item in backend.workspaces if item.container is selected)
        peer = next(item for item in backend.workspaces if item.container is not selected)
        owner = workspace._owner
        await asyncio.wait_for(entered.wait(), 3)
        with workspace_context(selected.workspace_id, selected.workspace_root,
                               session_catalog=selected.session_catalog):
            session = await selected.session_service.create(SessionCreateRequest(title="驻留扫描关闭"))
        record = await selected.workspace_activity_service.append(
            event_id="residency-shutdown", session_id=session.session_id,
            status="completed", summary="原驻留扫描结算前保留关闭依赖",
        )
        locator = selected.session_catalog.resolver.resolve_session_node(session.session_id)
        started = asyncio.get_running_loop().time()
        closing = asyncio.create_task(workspace.close() if outcome == "default-deadline" else backend.close())
        if outcome == "default-deadline":
            await asyncio.wait_for(peer.close(), 5)
        async with asyncio.timeout(5):
            while workspace.pending_close_stage != "runtime.shutdown" or not worker.cancelling() or not peer.closed:
                await asyncio.sleep(0)
        shutdown = service._shutdown_task
        assert shutdown is not None and not shutdown.done()
        assert service._sweep_task is worker and worker.cancelling() == 1
        if outcome == "default-deadline":
            with pytest.raises(TimeoutError, match="ThreadRuntime关闭等待超过 30 秒"):
                # 实际等待默认期限；外层仅防回归成无限等待，不取消原生命周期owner。
                await asyncio.wait_for(asyncio.shield(closing), 35)
            assert asyncio.get_running_loop().time() - started >= 30
        observer = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        observer.cancel("取消驻留关闭观察")
        done, _pending = await asyncio.wait((observer,), timeout=3)
        assert done == {observer}
        with pytest.raises(asyncio.CancelledError, match="取消驻留关闭观察"):
            await observer
        assert service._shutdown_task is shutdown and not shutdown.done()
        assert service._sweep_task is worker and not worker.done() and worker.cancelling() == 1
        assert workspace._owner is owner and not workspace.closed and peer.closed
        assert main.app.state.workspace_container_registry is backend.registry and locator.is_dir()
        activity = selected.workspace_activity_service
        assert not activity.store._database._closed and record in activity.store.read_activity_page().records
        with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
            selected.workspace_root / ".boxteam/attachments", exclusive=True,
        ):
            pytest.fail("驻留原扫描未结算不能归还维护锁")
        with pytest.raises(RuntimeError, match="正在关闭"):
            await service.start()
        finish.set()
        if outcome == "success":
            await asyncio.wait_for(asyncio.shield(closing), 5)
        else:
            with pytest.raises(BaseExceptionGroup) as caught:
                if outcome == "default-deadline":
                    await asyncio.wait_for(backend.close(), 5)
                else:
                    await asyncio.wait_for(asyncio.shield(closing), 5)
            failure, = failures
            assert isinstance(failure, IsADirectoryError) and worker.exception() is failure
            runtime_error, = caught.value.exceptions
            assert isinstance(runtime_error, ExceptionGroup) and runtime_error.exceptions == (failure,)
            assert workspace.pending_close_stage == "runtime.shutdown" and not workspace.closed
            assert workspace._owner is owner and service._sweep_task is None
            assert not activity.store._database._closed and locator.is_dir()
            with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
                selected.workspace_root / ".boxteam/attachments", exclusive=True,
            ):
                pytest.fail("原错交付后仍须同入口完成剩余关闭")
            await backend.close()
        assert backend.closed and workspace.closed and workspace._owner is owner and calls == [1, 2]
        assert main.app.state.workspace_container_registry is None
        cold = WorkspaceActivityService(workspace_root=selected.workspace_root)
        try:
            assert record in cold.store.read_activity_page().records
        finally:
            cold.close()
        with AttachmentMaintenanceLock(selected.workspace_root / ".boxteam/attachments", exclusive=True):
            pass
    finally:
        finish.set()
        await asyncio.wait((worker,), timeout=3)
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, *(task for task in (closing, observer) if task is not None), return_exceptions=True)
        await asyncio.gather(service.shutdown(), return_exceptions=True)
        await lifetime.__aexit__(None, None, None)
