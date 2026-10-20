from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.background_message_bus import BackgroundMessageBus
from app.core.exceptions import NotFoundError
from app.core.job_event_bus import JobEventBus
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.schemas.internal_v2.common import JobStatus, MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService, JobState
from app.services.business.message_service import MessageService
from app.services.business.session_service import SessionService
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.harness.python.run_context import TestRunContext
from tests.support.message_service import build_message_service
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class _InstantJobExecutor:
    def __init__(self) -> None:
        self.session_ids: list[str] = []
        self.hold = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, job: JobState) -> str:
        self.session_ids.append(job.session_id)
        if self.hold:
            self.started.set()
            await self.release.wait()
        return "done"


@dataclass(slots=True)
class _AdmissionHarness:
    workspace: CatalogWorkspaceContext
    session_service: SessionService
    job_service: JobService
    message_bus: BackgroundMessageBus
    executor: _InstantJobExecutor
    input_owner: MessageService
    saver: RolloutCheckpointSaver
    guard_calls: list[str]


@pytest.fixture
async def admission_harness() -> AsyncIterator[_AdmissionHarness]:
    run_context = TestRunContext.from_test_file(Path(__file__))
    if run_context.workspace_root.exists():
        shutil.rmtree(run_context.workspace_root)
    workspace = build_catalog_workspace(run_context.output_root)
    saver = RolloutCheckpointSaver(workspace.sessions_root)
    session_service = SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=workspace.workspace_id,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,

    )
    message_bus = BackgroundMessageBus(store=BackgroundWorkStore(sessions_dir=workspace.sessions_root))
    executor = _InstantJobExecutor()
    input_owner = build_message_service(
        workspace.sessions_root, checkpointer=saver,
        workspace_root=run_context.workspace_root,
    )
    guard_calls: list[str] = []

    def session_lifecycle_guard(session_id: str) -> None:
        guard_calls.append(session_id)
        session_service.assert_session_active(session_id)

    job_service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=executor,
        session_lifecycle_guard=session_lifecycle_guard,
        input_owner=input_owner,
        terminal_status_writer=saver,
    )
    try:
        yield _AdmissionHarness(
            workspace=workspace,
            session_service=session_service,
            job_service=job_service,
            message_bus=message_bus,
            executor=executor,
            input_owner=input_owner,
            saver=saver,
            guard_calls=guard_calls,
        )
    finally:
        pending_tasks = [
            job.task
            for job in job_service._jobs.values()
            if job.task is not None and not job.task.done()
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        workspace.close()


@pytest.mark.asyncio
async def test_catalog_delete_settles_active_and_queued_jobs_before_isolation(
    admission_harness: _AdmissionHarness,
) -> None:
    workspace = admission_harness.workspace
    service = admission_harness.job_service
    session_id = await workspace.create_session(
        "活跃任务删除排空", current_provider_id="test-provider",
    )
    thread_id = workspace.resolver.main_thread_id(session_id)
    admission_harness.executor.hold = True

    async def accept(content: str, message_id: str):
        identity = service.reserve_user_execution_identity()
        now = datetime.now(UTC)
        admission_harness.input_owner.accept_user_message(
            MessageDTO(
                message_id=message_id, session_id=session_id, thread_id=thread_id,
                role=MessageRole.user, content=content, created_at=now, updated_at=now,
            ),
            execution_identity=identity,
        )
        return await service.start_job(
            session_id, content, thread_id=thread_id, execution_binding=identity,
            message_id=message_id, message_created_at=now.isoformat(),
        )

    active = await accept("旧 active 输入", "msg-active-delete")
    await asyncio.wait_for(admission_harness.executor.started.wait(), timeout=3)
    queued = await accept("已接受的 queued 输入", "msg-queued-delete")
    assert service._jobs[queued.job_id].status == JobStatus.queued
    database = admission_harness.saver._storage.storage.index_path(
        session_id, thread_id=thread_id,
    )
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()
    terminal_rows: dict[str, str] = {}

    async def cleanup(current_session_id: str) -> None:
        assert current_session_id == session_id
        assert workspace.node(session_id).state == "deleting"
        drain_started.set()
        await release_drain.wait()
        await service.delete_session_jobs(session_id)
        assert database.is_file(), "canonical 终态必须先于物理隔离"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            terminal_rows.update(connection.execute(
                "SELECT job_id,outcome FROM executions WHERE job_id IN (?,?)",
                (active.job_id, queued.job_id),
            ).fetchall())
            assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone() == (2,)

    workspace.delete_service.set_session_drain_callback(cleanup)
    deletion = asyncio.create_task(service.run_sessions_delete_operation(
        [session_id],
        lambda: workspace.resolver.delete_subtree(
            idempotency_key="delete-active-and-queued", root_node_id=session_id,
        ),
    ))
    try:
        await asyncio.wait_for(drain_started.wait(), timeout=3)
        assert not deletion.done()
        assert workspace.node(session_id).state == "deleting"
        with pytest.raises(SessionDeletionPendingError, match="session_deletion_pending"):
            await service.start_job(
                session_id, "迟到输入", thread_id=thread_id,
                execution_binding=service.reserve_user_execution_identity(),
                message_id="msg-late-delete", message_created_at=datetime.now(UTC).isoformat(),
            )
        release_drain.set()
        result = await asyncio.wait_for(deletion, timeout=3)
        assert result.record_state == "completed"
        assert terminal_rows == {active.job_id: "cancelled", queued.job_id: "cancelled"}
        assert admission_harness.executor.session_ids == [session_id], "queued 输入不能在删除收尾中启动"
        assert service._pending_queue.ids(session_id) == ()
        assert all(job.session_id != session_id for job in service._jobs.values())
        with pytest.raises(KeyError):
            workspace.store.get_node(session_id)
    finally:
        release_drain.set()
        if not deletion.done():
            deletion.cancel()
        await asyncio.gather(deletion, return_exceptions=True)


@pytest.mark.asyncio
async def test_catalog_delete_drain_failure_rejects_late_admission(
    admission_harness: _AdmissionHarness,
) -> None:
    workspace = admission_harness.workspace
    session_service = admission_harness.session_service
    job_service = admission_harness.job_service
    message_bus = admission_harness.message_bus
    target_session_id = await workspace.create_session(
        "删除后准入拒绝",
        current_provider_id="test-provider",
    )
    active_session_id = await workspace.create_session(
        "删除失败不影响",
        current_provider_id="test-provider",
    )
    target_thread_id = workspace.resolver.main_thread_id(target_session_id)
    active_thread_id = workspace.resolver.main_thread_id(active_session_id)

    target_message = await message_bus.emit(target_session_id, target_thread_id, "default", "待清理消息", message_id="target-message")
    active_message = await message_bus.emit(active_session_id, active_thread_id, "default", "保留消息", message_id="active-message")
    cleanup_reads: list[str] = []

    async def close_bus_then_fail(session_id: str) -> None:
        assert session_id == target_session_id
        assert workspace.store.get_node(session_id).state == "deleting"
        assert workspace.session_dir(session_id).is_dir()
        message_bus.close_session(session_id)
        cleanup_session = await session_service.get(session_id)
        cleanup_reads.append(cleanup_session.session_id)
        raise RuntimeError("injected failure after close_session")

    workspace.delete_service.set_session_drain_callback(close_bus_then_fail)
    delete_key = "delete-admission-after-bus-close"

    with pytest.raises(RuntimeError, match="injected failure after close_session"):
        await job_service.run_sessions_delete_operation(
            [target_session_id],
            lambda: workspace.resolver.delete_subtree(
                idempotency_key=delete_key,
                root_node_id=target_session_id,
            ),
        )

    assert cleanup_reads == [target_session_id]
    assert workspace.store.get_node(target_session_id).state == "deleting"
    assert workspace.session_dir(target_session_id).is_dir()
    assert target_session_id not in job_service._deleting_sessions
    assert workspace.store.get_node(active_session_id).state == "active"
    with pytest.raises(SessionDeletionPendingError):
        await message_bus.list_messages(target_session_id, target_thread_id, "default")
    with message_bus._store.control(target_session_id, read_only=True) as control:
        assert control.list_background_messages(target_thread_id, "default") == [target_message]
    assert await message_bus.list_messages(active_session_id, active_thread_id, "default") == [
        active_message
    ]

    preparation_calls: list[str] = []

    async def prepare_target() -> str:
        preparation_calls.append(target_session_id)
        return "prepared"

    with pytest.raises(
        SessionDeletionPendingError,
        match="session_deletion_pending",
    ):
        await job_service.run_session_preparation(target_session_id, prepare_target)

    with pytest.raises(
        SessionDeletionPendingError,
        match="session_deletion_pending",
    ):
        await job_service.start_job(
            target_session_id,
            "late message",
            thread_id=target_thread_id,
            execution_binding=job_service.reserve_user_execution_identity(),
            message_id="msg-late-deleted-session",
            message_created_at=datetime.now(UTC).isoformat(),
        )

    # 模拟消息 preparation 已完成后，迟到 continuation 才进入真实 Job callee。
    deletion_record = workspace.store.get_subtree_delete_record(delete_key)
    with pytest.raises(
        SessionDeletionPendingError,
        match="session_deletion_pending",
    ):
        await job_service._start_job_prepared(
            target_session_id,
            "late prepared message",
            thread_id=target_thread_id,
            execution_binding=job_service.reserve_user_execution_identity(),
            message_id="msg-late-prepared-deleted-session",
            message_created_at=datetime.now(UTC).isoformat(),
        )

    assert preparation_calls == []
    assert not any(
        job.session_id == target_session_id
        for job in job_service._jobs.values()
    )
    assert job_service._pending_queue.ids(target_session_id) == ()
    with message_bus._store.control(target_session_id, read_only=True) as control:
        assert control.list_background_messages(target_thread_id, "default") == [target_message]
    assert workspace.store.get_subtree_delete_record(delete_key) == deletion_record

    assert session_service.assert_session_active(active_session_id) is None

    async def prepare_active() -> str:
        preparation_calls.append(active_session_id)
        return "active-prepared"

    assert (
        await job_service.run_session_preparation(active_session_id, prepare_active)
        == "active-prepared"
    )
    active_identity = job_service.reserve_user_execution_identity()
    active_created_at = datetime.now(UTC)
    admission_harness.input_owner.accept_user_message(
        MessageDTO(
            message_id="msg-active-session", session_id=active_session_id,
            thread_id=active_thread_id, role=MessageRole.user, content="active message",
            created_at=active_created_at, updated_at=active_created_at,
        ),
        execution_identity=active_identity,
    )
    active_dispatch = await job_service.start_job(
        active_session_id,
        "active message",
        thread_id=active_thread_id,
        execution_binding=active_identity,
        message_id="msg-active-session",
        message_created_at=active_created_at.isoformat(),
    )
    active_job = job_service._jobs[active_dispatch.job_id]
    assert active_job.session_id == active_session_id
    assert active_job.task is not None
    await asyncio.wait_for(active_job.task, timeout=2)
    assert active_job.status == JobStatus.completed

    assert preparation_calls == [active_session_id]
    assert admission_harness.guard_calls == [
        target_session_id,
        target_session_id,
        target_session_id,
        active_session_id,
        active_session_id,
        active_session_id,
    ]
    assert admission_harness.executor.session_ids == [active_session_id]
    assert await message_bus.list_messages(active_session_id, active_thread_id, "default") == [
        active_message
    ]


async def test_missing_session_guard_rejects_before_preparation(
    admission_harness: _AdmissionHarness,
) -> None:
    missing = "ses_019b9dffc29a779e8564d196f78bb0a6"
    with pytest.raises(NotFoundError, match="Session .* not found"):
        admission_harness.session_service.assert_session_active(missing)
    preparation_calls = []

    async def prepare() -> str:
        preparation_calls.append(missing)
        return "prepared"

    with pytest.raises(NotFoundError, match="Session .* not found"):
        await admission_harness.job_service.run_session_preparation(missing, prepare)
    assert preparation_calls == []
    assert admission_harness.job_service._session_preparations == {}
    assert admission_harness.job_service._jobs == {}
