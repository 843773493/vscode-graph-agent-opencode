"""通过真实消息准入/Job 入口验证持久 FIFO 与任务启动崩溃窗口。"""
from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import JobEventBus
from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.common import JobStatus, MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.unit.core.catalog_workspace_helper import (
    CatalogWorkspaceContext,
    build_catalog_workspace,
)


class HeldExecutor:
    def __init__(self) -> None:
        self.started: asyncio.Queue[str] = asyncio.Queue()

    async def run(self, job) -> str:
        job.progress_reporter("agent_loop_ready")
        self.started.put_nowait(job.job_id)
        await asyncio.Future()
        raise AssertionError("执行屏障只能由测试取消")


@dataclass
class QueueCase:
    workspace: CatalogWorkspaceContext
    session_id: str
    thread_id: str
    saver: RolloutCheckpointSaver
    messages: MessageService
    store: PendingRequestStore
    jobs: JobService
    executor: HeldExecutor

    def accept(self, text: str, policy="after_turn"):
        identity = self.jobs.reserve_user_execution_identity()
        now = datetime.now(UTC)
        message = MessageDTO(
            message_id=create_prefixed_id("msg"), session_id=self.session_id,
            thread_id=self.thread_id, role=MessageRole.user, content=text, created_at=now, updated_at=now,
        )
        frozen = ExecutionQueueInput(
            execution_binding=identity, message_id=message.message_id, agent_id="default",
            provider_id=None, delivery_policy=policy, gateway_id=None, request_id=None, created_at=now.isoformat(),
        )
        self.messages.accept_user_message(message, execution_identity=identity, queue_input=frozen)
        return message, identity

    async def start(self, message, identity, policy="after_turn"):
        return await self.jobs.start_job(
            self.session_id, message.content, thread_id=self.thread_id,
            execution_binding=identity, agent_id="default", message_id=message.message_id,
            message_created_at=message.created_at.isoformat(), delivery_policy=policy,
        )


@pytest.fixture
async def queue_case(request: pytest.FixtureRequest) -> AsyncIterator[QueueCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace_root = context.workspace_root
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    shutil.copytree(Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", workspace_root)
    workspace = build_catalog_workspace(context.output_root, workspace_id=load_or_create_workspace_id(context.workspace_root))
    session_id = await workspace.create_session("事务 FIFO 验证")
    thread_id = workspace.resolver.main_thread_id(session_id)
    saver = RolloutCheckpointSaver(sessions_dir=workspace.sessions_root)
    messages = MessageService(main_thread_resolver=workspace.resolver.main_thread_id, checkpointer=saver,
                              workspace_root=workspace.workspace_root)
    store = PendingRequestStore(sessions_dir=workspace.sessions_root, owner=saver,
                                input_reader=lambda reference: messages.accepted_job_message(reference, display=False))
    executor = HeldExecutor()
    jobs = JobService(job_event_bus=JobEventBus(), job_executor=executor, input_owner=messages,
                      pending_request_store=store, terminal_status_writer=saver,
                      session_lifecycle_guard=workspace.resolver.resolve_session_node_for_runtime)
    case = QueueCase(workspace, session_id, thread_id, saver, messages, store, jobs, executor)
    try:
        yield case
    finally:
        jobs.close_admission()
        with workspace_context(workspace.workspace_id, workspace.workspace_root):
            await jobs.force_interrupt_active(reason="test_queue_teardown")
        await jobs.drain_finalizations()
        workspace.close()


@pytest.mark.asyncio
async def test_acceptance_commit_is_already_a_cold_runnable_queue(queue_case: QueueCase) -> None:
    case = queue_case
    message, identity = case.accept("提交后尚未调用 start_job")
    restarted = RolloutCheckpointSaver(sessions_dir=case.workspace.sessions_root)
    snapshot = restarted.execution_queue_snapshot(case.session_id, case.thread_id)
    assert len(snapshot.records) == 1
    record = snapshot.records[0]
    assert record.phase == "queued" and record.cursor.admission.job_id == identity.job_id
    with restarted._storage.storage.read_connection(case.session_id, case.thread_id) as connection:
        assert connection.execute("SELECT commit_kind FROM storage_commits WHERE commit_id=?", (record.cursor.commit_id,)).fetchone() == ("acceptance",)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (1,)
    assert not (case.workspace.session_dir(case.session_id) / "pending_requests.json").exists()
    await case.jobs.list_pending(case.session_id)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == identity.job_id
    assert case.jobs._jobs[identity.job_id].message_id == message.message_id


async def test_loaded_runtime_discovers_acceptance_without_start_job(queue_case: QueueCase) -> None:
    case = queue_case
    first = case.accept("原执行")
    await case.start(*first)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == first[1].job_id
    running = case.jobs._jobs[first[1].job_id]
    task = running.task
    second = case.accept("模拟准入提交后请求断开")
    snapshot = await case.jobs.list_pending(case.session_id)
    assert [item.job_id for item in snapshot.requests] == [second[1].job_id]
    assert case.jobs._jobs[first[1].job_id] is running and running.task is task
    assert case.executor.started.empty()


@pytest.mark.asyncio
async def test_fifo_policy_and_cancel_use_original_admission_cursor(queue_case: QueueCase) -> None:
    case = queue_case
    first = case.accept("active")
    dispatch = await case.start(*first)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == dispatch.job_id
    second = case.accept("queued")
    third = case.accept("tail", "after_interrupt")
    await case.start(*second)
    await case.start(*third, policy="after_interrupt")
    snapshot = await case.jobs.list_pending(case.session_id)
    assert [item.job_id for item in snapshot.requests] == [second[1].job_id, third[1].job_id]
    records = case.saver.execution_queue_snapshot(case.session_id, case.thread_id).records
    assert [item.enqueue_sequence for item in snapshot.requests] == [item.cursor.commit_id for item in records if item.phase == "queued"]
    changed = await case.jobs.update_pending_policy(case.session_id, third[0].message_id,
        delivery_policy="after_tool_result", expected_snapshot_version=snapshot.snapshot_version)
    assert changed.snapshot_version > snapshot.snapshot_version
    with pytest.raises(RuntimeError, match="过期"):
        await case.jobs.update_pending_policy(case.session_id, third[0].message_id,
            delivery_policy="after_turn", expected_snapshot_version=snapshot.snapshot_version)
    removed = await case.jobs.remove_pending(case.session_id, second[0].message_id)
    assert [item.job_id for item in removed.requests] == [third[1].job_id]
    assert case.saver.execution_queue_record(case.session_id, case.thread_id, second[1].job_id).outcome == "cancelled"


@pytest.mark.asyncio
async def test_task_creation_failure_retains_claim_and_retries_original_job(queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = queue_case
    message, identity = case.accept("task window")
    original = case.jobs._start_job_task
    failed = False

    def fail_once(job):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected create_task failure")
        original(job)

    monkeypatch.setattr(case.jobs, "_start_job_task", fail_once)
    with pytest.raises(OSError, match="injected"):
        await case.start(message, identity)
    job = case.jobs._jobs[identity.job_id]
    frozen = case.saver.execution_queue_record(case.session_id, case.thread_id, job.job_id)
    assert frozen.phase == "started" and frozen.claim_generation == 1
    assert await case.store.claim(frozen) is None
    await case.jobs.list_pending(case.session_id)
    assert case.jobs._jobs[job.job_id] is job
    assert await asyncio.wait_for(case.executor.started.get(), 5) == job.job_id
    assert case.executor.started.empty()


@pytest.mark.asyncio
async def test_queue_publication_error_rolls_back_acceptance(queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = queue_case
    def reject(*args, **kwargs):
        raise OSError("injected queue publication failure")
    monkeypatch.setattr(case.saver._storage.executions, "publish_execution_queue_input", reject)
    with pytest.raises(OSError, match="injected"):
        case.accept("不得留下接受一半的请求")
    with case.saver._storage.storage.read_connection(case.session_id, case.thread_id) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_acceptances").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM execution_queue").fetchone() == (0,)


@pytest.mark.parametrize("loaded", [False, True])
async def test_delete_cancels_durable_entries_before_discarding_jobs(queue_case: QueueCase, loaded: bool) -> None:
    case = queue_case
    first = case.accept("删除前已接受")
    if loaded:
        await case.start(*first)
        assert await asyncio.wait_for(case.executor.started.get(), 5) == first[1].job_id
    second = case.accept("尚未调用 start_job")
    deleted = await case.jobs.run_session_delete_operation(
        case.session_id, lambda: case.jobs.delete_session_jobs(case.session_id),
    )
    assert deleted == 2
    assert case.saver.execution_queue_snapshot(case.session_id, case.thread_id).records == ()
    for _, identity in [first, second]:
        record = case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id)
        assert record.phase == "terminal" and record.outcome == "cancelled"
        assert identity.job_id not in case.jobs._jobs
    assert case.executor.started.empty()
    assert not case.jobs._thread_current_job


async def test_delete_queue_write_failure_keeps_active_task_and_remaining_fifo(
    queue_case: QueueCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = queue_case
    first = case.accept("保持原执行")
    await case.start(*first)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == first[1].job_id
    active = case.jobs._jobs[first[1].job_id]
    task = active.task
    original_status = active.status
    second = case.accept("先提交取消")
    third = case.accept("取消落盘失败")
    await case.start(*second)
    await case.start(*third)
    original_writer = case.saver.mark_job_terminal_status

    def fail_tail(**kwargs):
        if kwargs["job_id"] == third[1].job_id:
            raise OSError("injected delete queue terminal failure")
        return original_writer(**kwargs)

    monkeypatch.setattr(case.saver, "mark_job_terminal_status", fail_tail)
    with pytest.raises(OSError, match="injected delete queue terminal failure"):
        await case.jobs.run_session_delete_operation(
            case.session_id, lambda: case.jobs.delete_session_jobs(case.session_id),
        )
    assert case.session_id not in case.jobs._deleting_sessions
    assert active.status == original_status and active.cancellation_reason is None
    assert active.task is task and task is not None and not task.done()
    assert case.jobs._thread_current_job[(case.session_id, case.thread_id)] == first[1].job_id
    assert case.jobs._jobs[second[1].job_id].status == JobStatus.cancelled
    with pytest.raises(ValueError, match="队列中不存在"):
        case.jobs._pending_queue.entry(second[1].job_id)
    assert case.jobs._pending_queue.entry(third[1].job_id).job_id == third[1].job_id
    snapshot = await case.jobs.list_pending(case.session_id)
    assert [entry.job_id for entry in snapshot.requests] == [third[1].job_id]
    assert case.jobs._jobs[first[1].job_id] is active and active.task is task
    assert case.executor.started.empty()

    monkeypatch.setattr(case.saver, "mark_job_terminal_status", original_writer)
    assert await case.jobs.run_session_delete_operation(
        case.session_id, lambda: case.jobs.delete_session_jobs(case.session_id),
    ) == 3
    assert case.saver.execution_queue_snapshot(case.session_id, case.thread_id).records == ()
    for _, identity in [first, second, third]:
        record = case.saver.execution_queue_record(case.session_id, case.thread_id, identity.job_id)
        assert record.phase == "terminal" and record.outcome == "cancelled"
    assert case.executor.started.empty() and not case.jobs._thread_current_job
