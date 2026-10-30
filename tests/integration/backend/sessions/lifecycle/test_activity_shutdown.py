"""活动库原生关闭及线程池等待不能阻塞 Backend 的观察与依赖保留。"""

from __future__ import annotations

import asyncio
import inspect
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from typing import BinaryIO

import httpx
import pytest

from app import main
from app.core import sqlite_state
from app.core.path_utils import workspace_context
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.workspace_state_store import WorkspaceActivityService
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414
)


@pytest.mark.parametrize("phase", ["native", "pool"])
@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
async def test_activity_close_wait_preserves_owner_and_dependencies(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
    phase: str, interruption: str,
) -> None:
    deadlines: list[asyncio.Timeout] = []

    def capture_deadline(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        deadline = asyncio.timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(inspect.getmodule(main.BackendLifespan), "timeout", capture_deadline)
    token = secrets.token_hex(32)
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    entered, release = Event(), Event()
    pool = None
    occupied = None
    loop = asyncio.get_running_loop()
    original_mcp_close = McpConnectionService.close

    async def close_mcp(service: McpConnectionService) -> None:
        nonlocal occupied
        await original_mcp_close(service)
        if phase == "pool" and service is selected.mcp_connection_service:
            # 先核对独立挂载完成，再只在目标活动库的前序步骤占满线程池。
            await asyncio.shield(peer_lifetime._owner)
            assert pool is not None
            occupied = pool.submit(occupy_worker)
            loop.set_default_executor(pool)

    monkeypatch.setattr(McpConnectionService, "close", close_mcp)
    lifetime = main.lifespan(main.app)
    await lifetime.__aenter__()
    attempts: list[int] = []
    closing = None
    previous_pool = getattr(loop, "_default_executor")  # noqa: B009 - 测试恢复原池，公开事件循环类型未声明该属性。
    assert isinstance(previous_pool, ThreadPoolExecutor)
    try:
        peer, selected = mounted_case.containers
        with workspace_context(selected.workspace_id, selected.workspace_root, session_catalog=selected.session_catalog):
            session = await selected.session_service.create(SessionCreateRequest(title="活动库关闭"))
        activity = selected.workspace_activity_service
        record = await activity.append(
            event_id="activity-shutdown", session_id=session.session_id,
            status="completed", summary="保留原活动记录",
        )
        database = activity.store._database
        handle = database._ownership._lock_handle
        assert handle is not None and not handle.closed
        locator = selected.session_catalog.resolver.resolve_session_node(session.session_id)
        backend = main.app.state.workspace_lifespan
        registry = backend.registry
        selected_lifetime = next(item for item in backend.workspaces if item.container is selected)
        peer_lifetime = next(item for item in backend.workspaces if item.container is peer)
        original_unlock = sqlite_state.portalocker.unlock
        event_loop_thread = get_ident()

        def unlock(current: BinaryIO) -> None:
            if current is handle:
                attempts.append(get_ident())
                if phase == "native":
                    entered.set()
                    if not release.wait(12):
                        raise RuntimeError("原生关闭阻塞了事件循环，测试保护期限已到")
            original_unlock(current)

        monkeypatch.setattr(sqlite_state.portalocker, "unlock", unlock)
        if phase == "pool":
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="activity-shutdown")

            def occupy_worker() -> None:
                entered.set()
                if not release.wait(12):
                    raise RuntimeError("测试未释放原线程池任务")

        closing = asyncio.create_task(backend.close())
        async with asyncio.timeout(5):
            while not entered.is_set() or selected_lifetime.pending_close_stage != "activity.close":
                await asyncio.sleep(0.001)
        await asyncio.wait_for(asyncio.shield(peer_lifetime._owner), 3)
        assert peer_lifetime.closed and not selected_lifetime.closed
        assert not release.is_set() and not handle.closed
        if phase == "pool":
            assert attempts == [] and not database._closed
        else:
            assert len(attempts) == 1 and attempts[0] != event_loop_thread
            assert database._closed
        owner, workspace_owner = backend._close_owner, selected_lifetime._owner
        assert owner is not None and not owner.done() and len(deadlines) == 1
        if interruption == "deadline":
            deadlines[0].reschedule(loop.time())
            with pytest.raises(TimeoutError, match="stage=workspaces.*activity.close"):
                await asyncio.wait_for(closing, 3)
        else:
            closing.cancel("只取消关闭观察")
            with pytest.raises(asyncio.CancelledError, match="只取消关闭观察"):
                await asyncio.wait_for(closing, 3)
        assert backend._close_owner is owner and not owner.done()
        assert selected_lifetime._owner is workspace_owner and not workspace_owner.done()
        assert main.app.state.workspace_container_registry is registry and locator.is_dir()
        for container in mounted_case.containers:
            with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
                container.workspace_root / ".boxteam/attachments", exclusive=True,
            ):
                pytest.fail("活动库原关闭未完成时不能归还维护锁")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test") as client:
            response = await asyncio.wait_for(client.get(
                "/api/v1/internal/runtime-logging", headers={"X-Local-Token": token},
            ), 3)
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        retry = asyncio.create_task(backend.close())
        await asyncio.sleep(0)
        assert backend._close_owner is owner and not handle.closed
        release.set()
        await asyncio.wait_for(retry, 5)
        assert backend.closed and selected_lifetime.closed and handle.closed
        assert len(attempts) == 1 and attempts[0] != event_loop_thread
        assert selected_lifetime._owner is workspace_owner and backend._close_owner is owner
        assert main.app.state.workspace_lifespan is None and locator.is_dir()
        # 冷 owner 仍读取同一数据库中的原始活动事实。
        cold = WorkspaceActivityService(workspace_root=selected.workspace_root)
        try:
            assert record in cold.store.read_activity_page().records
        finally:
            cold.close()
        for container in mounted_case.containers:
            with AttachmentMaintenanceLock(container.workspace_root / ".boxteam/attachments", exclusive=True):
                pass
    finally:
        release.set()
        loop.set_default_executor(previous_pool)
        if occupied is not None:
            occupied.result(timeout=3)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await lifetime.__aexit__(None, None, None)
        if pool is not None:
            pool.shutdown(wait=True)


async def test_activity_native_close_failure_retries_original_stage(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifetime = main.lifespan(main.app)
    await lifetime.__aenter__()
    selected = mounted_case.containers[-1]
    backend = main.app.state.workspace_lifespan
    workspace = next(item for item in backend.workspaces if item.container is selected)
    handle = selected.workspace_activity_service.store._database._ownership._lock_handle
    original_unlock = sqlite_state.portalocker.unlock
    failure = RuntimeError("活动库原生解锁失败")
    attempts = 0

    def unlock(current: BinaryIO) -> None:
        nonlocal attempts
        if current is handle:
            attempts += 1
            raise failure
        original_unlock(current)

    monkeypatch.setattr(sqlite_state.portalocker, "unlock", unlock)
    try:
        with pytest.raises(BaseExceptionGroup) as caught:
            await backend.close()
        assert caught.value.exceptions == (failure,)
        assert workspace.pending_close_stage == "activity.close" and not workspace.closed
        assert attempts == 1
        assert selected.workspace_activity_service.store._database._closed
        assert main.app.state.workspace_container_registry is backend.registry
        owner = workspace._owner
        # 原SQLite owner已由finally关闭句柄；重试只完成原阶段，不重复原生释放。
        await backend.close()
        assert backend.closed and workspace._owner is owner and attempts == 1
    finally:
        monkeypatch.setattr(sqlite_state.portalocker, "unlock", original_unlock)
        await lifetime.__aexit__(None, None, None)
