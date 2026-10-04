from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.background_message_bus import BackgroundMessageBus
from app.core.job_event_bus import JobEventBus
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import JobService, JobState
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.harness.python.run_context import TestRunContext
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class _InstantJobExecutor:
    def __init__(self) -> None:
        self.session_ids: list[str] = []

    async def run(self, job: JobState) -> str:
        self.session_ids.append(job.session_id)
        return "done"


@dataclass(slots=True)
class _AdmissionHarness:
    workspace: CatalogWorkspaceContext
    session_service: SessionService
    job_service: JobService
    message_bus: BackgroundMessageBus
    executor: _InstantJobExecutor
    guard_calls: list[str]


@pytest.fixture
async def admission_harness() -> AsyncIterator[_AdmissionHarness]:
    run_context = TestRunContext.from_test_file(Path(__file__))
    if run_context.workspace_root.exists():
        shutil.rmtree(run_context.workspace_root)
    workspace = build_catalog_workspace(run_context.output_root)
    session_service = SessionService(
        config_service=ConfigService(),
        trace_event_store=TraceEventStore(sessions_dir=workspace.sessions_root),
        workspace_id=workspace.workspace_id,
        path_resolver=workspace.resolver,
        creation_service=workspace.creation_service,
    )
    message_bus = BackgroundMessageBus()
    executor = _InstantJobExecutor()
    guard_calls: list[str] = []

    def session_lifecycle_guard(session_id: str) -> None:
        guard_calls.append(session_id)
        session_service.assert_session_active(session_id)

    job_service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=executor,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    try:
        yield _AdmissionHarness(
            workspace=workspace,
            session_service=session_service,
            job_service=job_service,
            message_bus=message_bus,
            executor=executor,
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

    message_bus.emit(target_session_id, "default", "待清理消息")
    active_message = message_bus.emit(active_session_id, "default", "保留消息")
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
    assert await message_bus.list_messages(target_session_id, "default") == []
    assert await message_bus.list_messages(active_session_id, "default") == [
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
            job_id="job-late-deleted-session",
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
            job_id="job-late-prepared-deleted-session",
            message_id="msg-late-prepared-deleted-session",
            message_created_at=datetime.now(UTC).isoformat(),
        )

    assert preparation_calls == []
    assert not any(
        job.session_id == target_session_id
        for job in job_service._jobs.values()
    )
    assert job_service._pending_queue.ids(target_session_id) == ()
    assert not any(
        owner_session_id == target_session_id
        for owner_session_id, _agent_id in message_bus._messages
    )
    assert workspace.store.get_subtree_delete_record(delete_key) == deletion_record

    assert session_service.assert_session_active(active_session_id) is None

    async def prepare_active() -> str:
        preparation_calls.append(active_session_id)
        return "active-prepared"

    assert (
        await job_service.run_session_preparation(active_session_id, prepare_active)
        == "active-prepared"
    )
    active_dispatch = await job_service.start_job(
        active_session_id,
        "active message",
        job_id="job-active-session",
        message_id="msg-active-session",
        message_created_at=datetime.now(UTC).isoformat(),
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
    assert await message_bus.list_messages(active_session_id, "default") == [
        active_message
    ]
