"""活动裁剪的存储锁与线程池等待不能占住生命周期事件循环。"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident

import httpx
import pytest

from app import main, workspace_lifecycle
from app.core.path_utils import workspace_context
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.log_service import LogSnapshotRecord
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

pytestmark = pytest.mark.usefixtures("maintenance_case")


@pytest.fixture
def prune_tick(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    wake = asyncio.Event()
    ticked: set[asyncio.Task[object]] = set()
    original_sleep = asyncio.sleep

    async def sleep(seconds: float) -> None:
        task = asyncio.current_task()
        if (seconds == 3600 and task is not None and task not in ticked
                and task.get_name().startswith("workspace-activity-prune:")):
            ticked.add(task)
            await wake.wait()
        else:
            await original_sleep(seconds)

    monkeypatch.setattr(workspace_lifecycle.asyncio, "sleep", sleep)
    return wake


@pytest.mark.parametrize("phase", ["sqlite", "pool"])
@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
async def test_periodic_prune_wait_keeps_original_task_and_dependencies(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
    prune_tick: asyncio.Event, phase: str, interruption: str,
) -> None:
    deadlines: list[asyncio.Timeout] = []
    token = secrets.token_hex(32)
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)

    def capture_deadline(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        deadline = asyncio.timeout(seconds)
        deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(inspect.getmodule(main.BackendLifespan), "timeout", capture_deadline)
    lifetime = main.lifespan(main.app)
    _main_lifespan_state = await lifetime.__aenter__()
    await _main_lifespan_state["after_http_startup"]()
    selected = mounted_case.containers[-1]
    activity = selected.workspace_activity_service
    backend = main.app.state.workspace_lifespan
    workspace = next(item for item in backend.workspaces if item.container is selected)
    periodic = next(task for task in workspace._periodic_tasks
                    if task.get_name() == f"workspace-activity-prune:{selected.workspace_id}")
    loop = asyncio.get_running_loop()
    previous_pool = getattr(loop, "_default_executor")  # noqa: B009 - 测试恢复原池，不改变产品配置。
    pool = occupied = writer = closing = retry = None
    entered, release = Event(), Event()
    calls: list[int] = []
    original_prune = activity.store.prune_activity

    def prune(*, retention_days: int) -> int:
        calls.append(get_ident())
        entered.set()
        return original_prune(retention_days=retention_days)

    def occupy_worker() -> None:
        entered.set()
        if not release.wait(12):
            raise RuntimeError("测试未释放活动裁剪线程池")

    try:
        with workspace_context(selected.workspace_id, selected.workspace_root, session_catalog=selected.session_catalog):
            session = await selected.session_service.create(SessionCreateRequest(title="活动裁剪等待"))
        locator = selected.session_catalog.resolver.resolve_session_node(session.session_id)
        old = await activity.append(
            event_id="expired", session_id=session.session_id, status="completed", summary="应裁剪",
            occurred_at=(datetime.now(UTC) - timedelta(days=40)).isoformat(),
        )
        retained = await activity.append(
            event_id="retained", session_id=session.session_id, status="completed", summary="应保留",
        )
        monkeypatch.setattr(activity.store, "prune_activity", prune)
        if phase == "sqlite":
            # 真实SQLite写事务阻止DELETE；读仍可进行，不用假存储替代裁剪。
            writer = activity.store._database.connection()
            writer.execute("BEGIN IMMEDIATE")
        else:
            assert isinstance(previous_pool, ThreadPoolExecutor)
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="activity-prune")
            occupied = pool.submit(occupy_worker)
            loop.set_default_executor(pool)
        prune_tick.set()
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
        assert not periodic.done(), "裁剪仍应等待原SQLite写锁或线程池"
        assert len(calls) == (0 if phase == "pool" else 1)
        assert get_ident() not in calls
        closing = asyncio.create_task(backend.close())
        async with asyncio.timeout(2):
            while not periodic.cancelling() or not deadlines:
                await asyncio.sleep(0.001)
        assert workspace.pending_close_stage == "periodic_tasks"
        owner = backend._close_owner
        if interruption == "deadline":
            deadlines[0].reschedule(loop.time())
            with pytest.raises(TimeoutError, match="periodic_tasks"):
                await asyncio.wait_for(closing, 2)
        else:
            closing.cancel("只取消活动裁剪的关闭观察")
            with pytest.raises(asyncio.CancelledError, match="只取消活动裁剪的关闭观察"):
                await asyncio.wait_for(closing, 2)
        assert not periodic.done() and not activity.store._database._closed
        assert backend._close_owner is owner and not owner.done()
        assert main.app.state.workspace_container_registry is backend.registry and locator.is_dir()
        with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(
            selected.workspace_root / ".boxteam/attachments", exclusive=True,
        ):
            pytest.fail("原裁剪未结算前不能释放工作区维护锁")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://workspace.test") as client:
            response = await asyncio.wait_for(client.get(
                "/api/v1/internal/runtime-logging", headers={"X-Local-Token": token},
            ), 2)
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        retry = asyncio.create_task(backend.close())
        await asyncio.sleep(0)
        assert backend._close_owner is owner and not periodic.done()
        release.set()
        if writer is not None:
            writer.rollback()
        await asyncio.wait_for(retry, 5)
        assert backend.closed and periodic.cancelled() and len(calls) == 1
        assert calls[0] != get_ident() and activity.store._database._closed
        cold = WorkspaceActivityService(workspace_root=selected.workspace_root)
        try:
            records = cold.store.read_activity_page().records
            assert retained in records and old not in records
        finally:
            cold.close()
        with AttachmentMaintenanceLock(selected.workspace_root / ".boxteam/attachments", exclusive=True):
            pass
    finally:
        release.set()
        if writer is not None:
            writer.rollback()
            writer.close()
        if previous_pool is not None:
            loop.set_default_executor(previous_pool)
        await asyncio.gather(*(task for task in (closing, retry) if task is not None), return_exceptions=True)
        try:
            await lifetime.__aexit__(None, None, None)
        finally:
            # 红测中的真实SQLITE_BUSY也必须交回原关闭链，随后重入完成资源清理。
            if not backend.closed:
                await backend.close()
            if pool is not None:
                assert occupied is not None
                occupied.result(timeout=3)
                pool.shutdown(wait=True)


async def test_startup_prune_runs_outside_event_loop(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tuple(mounted_case.roots.values())[-1]
    entered, release = Event(), Event()
    calls: list[int] = []
    original_prune = WorkspaceActivityService.prune

    def prune(service: WorkspaceActivityService) -> int:
        if service.store.workspace_root == target:
            calls.append(get_ident())
            entered.set()
            if not release.wait(8):
                raise RuntimeError("启动裁剪占住了事件循环，测试保护期限已到")
        return original_prune(service)

    monkeypatch.setattr(WorkspaceActivityService, "prune", prune)
    lifetime = main.lifespan(main.app)
    _main_lifespan_state = await lifetime.__aenter__()
    starting = asyncio.create_task(_main_lifespan_state["after_http_startup"]())
    try:
        async with asyncio.timeout(15):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        assert len(calls) == 1 and calls[0] != get_ident()
        assert not starting.done(), "存储未结算前不能发布启动成功"
        release.set()
        await asyncio.wait_for(starting, 5)
    finally:
        release.set()
        await starting
        await lifetime.__aexit__(None, None, None)


async def test_periodic_retention_in_mounted_workspace(
    mounted_case: MountedCase, prune_tick: asyncio.Event,
) -> None:
    lifetime = main.lifespan(main.app)
    state = await lifetime.__aenter__()
    try:
        await state["after_http_startup"]()
        selected = mounted_case.containers[-1]
        with workspace_context(selected.workspace_id, selected.workspace_root, session_catalog=selected.session_catalog):
            session = await selected.session_service.create(SessionCreateRequest(title="周期详情回收"))
            empty = await selected.session_service.create(SessionCreateRequest(title="不物化空线程"))
        resolver = selected.session_service.path_resolver
        empty_rollout = resolver.resolve_thread_node(empty.session_id, resolver.main_thread_id(empty.session_id)) / "rollout"
        assert not empty_rollout.exists()
        thread = resolver.main_thread_id(session.session_id)
        detail_paths = []
        for namespace, days in (("", 0), ("nested", 0), ("nested", 30)):
            record = selected.rollout_checkpoint_runtime.context_plan_detail_store.write(
                session_id=session.session_id, thread_id=thread,
                assembly_id=f"retention-{namespace}-{days}", detail_kind="assembly_snapshot",
                retention_class="assembly_audit", visibility="internal", detail={"retention_days": days},
                retention_days=days, checkpoint_ns=namespace,
            )
            selected.rollout_checkpoint_runtime.storage.assemblies.register_context_plan_details((record,))
            path = resolver.resolve_thread_node(session.session_id, thread) / record.relative_path
            assert path.is_file()
            detail_paths.append(path)
        snapshot = await selected.log_service.write_html_snapshot(LogSnapshotRecord(session_id=None, html="过期快照"))
        metadata = selected.workspace_root / f".boxteam/logs/webview/{snapshot.snapshot_id}.json"
        payload = json.loads(metadata.read_text())
        payload["created_at"] = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        metadata.write_text(json.dumps(payload))
        store = selected.workspace_activity_service.store
        for event_id, domain in (("expired", "workspace"), ("fresh", "workspace"), ("other", "other")):
            store.append_config_event(
                event_id=event_id, config_domain=domain, candidate_id=None, attempt_id=None,
                apply_id=None, idempotency_key=None, commit_revision=None,
                active_revision=None, pending_revision=None, source="watcher", result="applied",
            )
        store.create_pending_config_candidate(
            config_domain="workspace", candidate_id="current", idempotency_key="current",
            payload={}, source_baseline={}, candidate_digest="candidate", effective_digest="effective",
            target_generation=None, fencing_token=None, state="candidate_validated",
        )
        store.append_config_event(
            event_id="diagnostic", config_domain="workspace", candidate_id="current", attempt_id=None,
            apply_id=None, idempotency_key=None, commit_revision=None, active_revision=None,
            pending_revision=None, source="watcher", result="applied", applied_paths=("/logging",),
        )
        connection = store._database.connection()
        try:
            connection.execute(
                "UPDATE config_events SET occurred_at=? WHERE event_id IN ('expired','other','diagnostic')",
                ((datetime.now(UTC) - timedelta(days=40)).isoformat(),),
            )
        finally:
            connection.close()
        prune_tick.set()
        async with asyncio.timeout(5):
            while any(event.event_id == "expired" for event in store.list_config_events(config_domain="workspace")):
                await asyncio.sleep(0.01)
        assert [event.event_id for event in store.list_config_events(config_domain="workspace")] == ["fresh", "diagnostic"]
        assert mounted_case.containers[-1].config_service.get_reload_status().applied_paths == ("/logging",)
        assert [event.event_id for event in store.list_config_events(config_domain="other")] == ["other"]
        async with asyncio.timeout(5):
            while metadata.exists():
                await asyncio.sleep(0.01)
        assert not metadata.with_suffix(".html").exists()
        async with asyncio.timeout(5):
            while any(path.exists() for path in detail_paths[:2]):
                await asyncio.sleep(0.01)
        assert detail_paths[-1].is_file()
        assert not empty_rollout.exists()
    finally:
        await lifetime.__aexit__(None, None, None)
