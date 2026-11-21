"""活动裁剪的存储锁与线程池等待不能占住生命周期事件循环。"""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
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
from app.services.infrastructure.html_snapshot_store import HtmlSnapshotInput
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


@pytest.fixture(autouse=True)
def exclude_unrelated_custom_tools(maintenance_case: MountedCase) -> None:
    """周期维护验收不装配与被测生命周期无关的自定义模型工具。"""
    for root in maintenance_case.roots.values():
        path = root / ".boxteam/workspace.jsonc"
        config = json.loads(path.read_text())
        for agent in config["agents"].values():
            tools = agent.get("tools")
            if isinstance(tools, dict):
                tools.pop("custom", None)
        path.write_text(json.dumps(config, ensure_ascii=False) + "\n")


@pytest.fixture
def maintenance_ticks(monkeypatch: pytest.MonkeyPatch) -> dict[str, asyncio.Event]:
    ticks = {
        task_id: asyncio.Event()
        for task_id in (
            "workspace-activity-prune",
            "workspace-config-events-prune",
            "workspace-html-snapshot-prune",
            "workspace-context-plan-detail-prune",
        )
    }
    original_sleep = asyncio.sleep

    async def sleep(seconds: float) -> None:
        task = asyncio.current_task()
        task_id = task.get_name().rsplit(":maintenance:", 1)[-1] if task is not None else None
        wake = ticks.get(task_id) if seconds == 3600 else None
        if wake is not None:
            await wake.wait()
            wake.clear()
        else:
            await original_sleep(seconds)

    monkeypatch.setattr(workspace_lifecycle.asyncio, "sleep", sleep)
    return ticks


@pytest.mark.parametrize("phase", ["sqlite", "pool"])
@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
async def test_periodic_prune_wait_keeps_original_task_and_dependencies(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
    maintenance_ticks: dict[str, asyncio.Event], phase: str, interruption: str,
) -> None:
    deadlines: list[asyncio.Timeout] = []
    token = secrets.token_hex(32)
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    monkeypatch.setattr(main, "_model_stream_controller", None)

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
    periodic = next(task for task, label in workspace._maintenance_scope._tasks.items()
                    if label == "maintenance:workspace-activity-prune")
    loop = asyncio.get_running_loop()
    pool = occupied = writer = closing = retry = None
    worker_submitted = Event()
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
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="activity-prune")
            occupied = pool.submit(occupy_worker)
            original_to_thread = asyncio.to_thread
            expected_prune = activity.prune

            async def observe_worker_submission(function, /, *args, **kwargs):
                if getattr(function, "__name__", "") == "invoke":
                    operation = inspect.getclosurevars(function).nonlocals.get("operation")
                    if operation == expected_prune:
                        worker_submitted.set()
                        context = copy_context()
                        return await loop.run_in_executor(pool, context.run, function, *args)
                return await original_to_thread(function, *args, **kwargs)

            monkeypatch.setattr(asyncio, "to_thread", observe_worker_submission)
        maintenance_ticks["workspace-activity-prune"].set()
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
            if phase == "pool":
                while not worker_submitted.is_set():
                    await asyncio.sleep(0.001)
            else:
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
    mounted_case: MountedCase, maintenance_ticks: dict[str, asyncio.Event],
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
        snapshot = await selected.html_snapshot_store.write_html_snapshot(HtmlSnapshotInput(session_id=None, html="过期快照"))
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
        for task_id in (
            "workspace-activity-prune",
            "workspace-config-events-prune",
            "workspace-html-snapshot-prune",
            "workspace-context-plan-detail-prune",
        ):
            maintenance_ticks[task_id].set()
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


async def test_startup_failure_after_periodic_setup_drains_real_sqlite_worker(
    mounted_case: MountedCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    worker_entered = asyncio.Event()
    original_start_maintenance = workspace_lifecycle.start_maintenance
    original_create_task = asyncio.create_task
    startup_failure = RuntimeError("维护任务装配后的启动步骤失败")
    writer = None
    closing = cancelled_waiter = None
    lifetime = main.lifespan(main.app)
    state = await lifetime.__aenter__()
    backend = main.app.state.workspace_lifespan
    target_workspace_id = tuple(mounted_case.roots)[-1]
    workspace = next(
        item for item in backend.workspaces
        if item.container.workspace_id == target_workspace_id
    )
    selected = workspace.container
    activity = selected.workspace_activity_service
    original_prune_activity = activity.store.prune_activity

    def observe_prune_activity(*, retention_days: int = 30) -> int:
        loop.call_soon_threadsafe(worker_entered.set)
        return original_prune_activity(retention_days=retention_days)

    def start_maintenance(tasks, *, scope, identity, report_error) -> None:
        nonlocal writer
        if identity == selected.workspace_id:
            writer = sqlite3.connect(activity.store._database.path, timeout=0, isolation_level=None)
            writer.execute("BEGIN IMMEDIATE")
            activity.store.prune_activity = observe_prune_activity
            tasks = tuple(
                replace(task, run_immediately=True)
                if task.id == "workspace-activity-prune" else task
                for task in tasks
            )
        original_start_maintenance(
            tasks, scope=scope, identity=identity, report_error=report_error,
        )

    def fail_followup_task(coroutine, *, name=None, context=None):
        if name == f"fork-export-cleanup:{selected.workspace_id}":
            coroutine.close()
            raise startup_failure
        return original_create_task(coroutine, name=name, context=context)

    monkeypatch.setattr(workspace_lifecycle, "start_maintenance", start_maintenance)
    monkeypatch.setattr(asyncio, "create_task", fail_followup_task)
    try:
        with pytest.raises(RuntimeError, match="维护任务装配后的启动步骤失败") as startup:
            await state["after_http_startup"]()
        assert startup.value is startup_failure
        await asyncio.wait_for(worker_entered.wait(), 3)
        task = next(
            task for task, label in workspace._maintenance_scope._tasks.items()
            if label == "maintenance:workspace-activity-prune"
        )
        closing = original_create_task(backend.close())
        cancelled_waiter = original_create_task(backend.close())
        async with asyncio.timeout(3):
            while task.cancelling() == 0:
                await asyncio.sleep(0)
        cancelled_waiter.cancel("仅取消第二个关闭观察者")
        with pytest.raises(asyncio.CancelledError, match="仅取消第二个关闭观察者"):
            await cancelled_waiter
        assert workspace.pending_close_stage == "periodic_tasks"
        assert not activity.store._database._closed
        assert not backend.closed

        # 保持 SQLite 写锁，直到真实 DELETE 在 worker 线程中以 busy 超时失败。
        with pytest.raises(BaseExceptionGroup) as close_failure:
            await asyncio.wait_for(asyncio.shield(closing), 7)
        pending = [close_failure.value]
        database_errors = []
        while pending:
            error = pending.pop()
            if isinstance(error, sqlite3.OperationalError):
                database_errors.append(error)
            if error.__cause__ is not None:
                pending.append(error.__cause__)
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
        assert database_errors
        assert any(error.sqlite_errorcode == sqlite3.SQLITE_BUSY for error in database_errors)
        assert workspace.pending_close_stage == "periodic_tasks"
        assert not activity.store._database._closed
        assert not backend.closed

        writer.rollback()
        writer.close()
        writer = None
        await asyncio.wait_for(backend.close(), 5)
        assert workspace.closed and backend.closed and activity.store._database._closed
    finally:
        if writer is not None:
            writer.rollback()
            writer.close()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        if cancelled_waiter is not None:
            await asyncio.gather(cancelled_waiter, return_exceptions=True)
        if not backend.closed:
            await backend.close()
        await lifetime.__aexit__(None, None, None)
