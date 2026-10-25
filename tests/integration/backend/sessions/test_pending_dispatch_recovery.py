"""产品准入生成队列后重开调度器，验证硬错误重试不重放 sibling。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import JobEventBus
from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.common import JobStatus, MessageRole
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.message import MessageDTO
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from tests.harness.python.run_context import TestRunContext
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace


class _HeldExecutor:
    """停在真实 Job 执行端口；此项验证调度，不验证 Provider。"""

    def __init__(self) -> None:
        self.started: asyncio.Queue[str] = asyncio.Queue()

    async def run(self, job: JobRuntimeStateProtocol) -> str:
        assert job.progress_reporter is not None
        job.progress_reporter("agent_loop_ready")
        self.started.put_nowait(job.job_id)
        await asyncio.Future()
        raise AssertionError("执行屏障不应自行结束")


async def _stop_jobs(jobs: JobService) -> None:
    jobs.close_admission()
    tasks = [job.task for job in jobs._jobs.values() if job.task is not None]
    # 用产品暂停使旧执行端口安静退出；取消会按既有语义启动下一项。
    for job in jobs._jobs.values():
        if job.task is not None and not job.task.done():
            await jobs.control(job.job_id, JobControlRequest(action="pause"))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
    await jobs.drain_finalizations()


@dataclass(frozen=True)
class PendingRecoveryCase:
    session_id: str
    threads: tuple[str, str]
    queued: tuple[tuple[str, str], tuple[str, str]]
    jobs: JobService
    executor: _HeldExecutor
    store: PendingRequestStore
    workspace_id: str
    workspace_root: Path


@pytest.fixture
async def pending_recovery_case(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PendingRecoveryCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace_root = context.workspace_root
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = build_catalog_workspace(
        workspace_root, workspace_id=load_or_create_workspace_id(workspace_root),
    )
    session_id = await workspace.create_session("多线程队列恢复")
    factory = OwnerThreadCreationFactory(
        sessions_root=workspace.sessions_root, workspace_id=workspace.workspace_id,
        path_resolver=workspace.resolver,
    )
    profile = {**DEEP_AGENT_CAPABILITY_PROFILE, "thread_role": "child"}
    with factory.for_owner_session(session_id) as thread_owner:
        child = await thread_owner.create(
            idempotency_key="pending-child", session_id=session_id, thread_id=None,
            delegation_id=None, initial_state="idle",
            session_metadata={
                "graph_binding": {**asdict(DEEP_AGENT_GRAPH_BINDING),
                                  "capability_profile_hash": compute_capability_profile_hash(capability_profile=profile)},
                "capability_profile": profile, "task_seed": None, "task_reference": None,
            }, artifact_manifest={},
        )
    threads = (workspace.resolver.main_thread_id(session_id), child.child_thread_id)
    saver = RolloutCheckpointSaver(sessions_dir=workspace.sessions_root)
    messages = MessageService(
        main_thread_resolver=workspace.resolver.main_thread_id, checkpointer=saver,
        workspace_root=workspace_root,
    )
    store = PendingRequestStore(
        sessions_dir=workspace.sessions_root,
        owner=saver, input_reader=lambda reference: messages.accepted_job_message(reference, display=False),
    )

    def scheduler(executor: _HeldExecutor) -> JobService:
        return JobService(
            job_event_bus=JobEventBus(), job_executor=executor,
            pending_request_store=store, input_owner=messages, terminal_status_writer=saver,
            session_lifecycle_guard=workspace.resolver.resolve_session_node_for_runtime,
        )

    source_executor = _HeldExecutor()
    source = scheduler(source_executor)
    cold_executor = _HeldExecutor()
    cold = scheduler(cold_executor)
    try:
        queued = []
        for thread_id in threads:
            identities = []
            for index in range(3):
                identity = source.reserve_user_execution_identity()
                now = datetime.now(UTC)
                message = MessageDTO(
                    message_id=create_prefixed_id("msg"), session_id=session_id,
                    thread_id=thread_id, role=MessageRole.user, content=f"排队 {index}",
                    created_at=now, updated_at=now,
                )
                messages.accept_user_message(message, execution_identity=identity, queue_input=ExecutionQueueInput(
                    execution_binding=identity, message_id=message.message_id, agent_id="default", provider_id=None,
                    delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=now.isoformat(),
                ))
                dispatch = await source.start_job(
                    session_id, message.content, thread_id=thread_id,
                    execution_binding=identity, message_id=message.message_id,
                    message_created_at=now.isoformat(),
                )
                if index == 0:
                    assert await asyncio.wait_for(source_executor.started.get(), 5) == dispatch.job_id
                else:
                    assert dispatch.job_status == "queued"
                    identities.append(dispatch.job_id)
            queued.append(tuple(identities))
        # 只关闭旧运行面；队列正文和控制引用全部来自上面的真实准入与派发入口。
        await _stop_jobs(source)
        # 通过产品取消终结占位执行；旧运行面停派发，保留其后的真实等待项。
        async def stopped_dispatch(*_args, **_kwargs):
            return False
        monkeypatch.setattr(source, "_start_next_pending", stopped_dispatch)
        for state in tuple(source._jobs.values()):
            if state.status == JobStatus.paused:
                await source.control(state.job_id, JobControlRequest(action="cancel"))
        assert [item.job_id for item in await store.load(session_id)] == [job for group in queued for job in group]
        yield PendingRecoveryCase(
            session_id, threads, tuple(queued), cold, cold_executor, store,
            workspace.workspace_id, workspace_root,
        )
    finally:
        await _stop_jobs(source)
        await _stop_jobs(cold)
        workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["load_projection", "first_dispatch_projection", "sibling_dispatch_projection", "sibling_task"])
async def test_real_pending_queue_retries_without_replacing_or_replaying_sibling(
    pending_recovery_case: PendingRecoveryCase, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    case = pending_recovery_case
    original_save = case.jobs._pending_requests.refresh_version
    original_start = case.jobs._start_job_task
    save_calls = 0
    starts: list[str] = []
    failing_save = {"load_projection": 1, "first_dispatch_projection": 2, "sibling_dispatch_projection": 3}.get(failure)
    fail_task = failure == "sibling_task"

    async def save_once(session_id):
        nonlocal save_calls
        save_calls += 1
        if save_calls == failing_save:
            raise OSError("injected queue persistence failure")
        await original_save(session_id)

    def start_once(job):
        nonlocal fail_task
        if fail_task and job.thread_id == case.threads[1]:
            fail_task = False
            raise OSError("injected sibling task failure")
        starts.append(job.job_id)
        original_start(job)

    monkeypatch.setattr(case.jobs._pending_requests, "refresh_version", save_once)
    monkeypatch.setattr(case.jobs, "_start_job_task", start_once)
    with pytest.raises(OSError, match="injected"):
        await case.jobs.list_pending(case.session_id)
    initial = dict(case.jobs._jobs)
    initial_tasks = {key: job.task for key, job in initial.items() if job.task is not None}
    if failure.startswith("sibling"):
        assert starts == [case.queued[0][0]]
        assert list(initial_tasks) == [case.queued[0][0]]
    snapshot = await case.jobs.list_pending(case.session_id)
    assert starts == [group[0] for group in case.queued]
    assert all(case.jobs._jobs[key] is job for key, job in initial.items())
    assert all(case.jobs._jobs[key].task is task for key, task in initial_tasks.items())
    assert [item.job_id for item in snapshot.requests] == [group[1] for group in case.queued]
    assert [item.job_id for item in await case.store.load(case.session_id)] == [group[1] for group in case.queued]
    for group in case.queued:
        assert await asyncio.wait_for(case.executor.started.get(), 5) == group[0]
    assert case.executor.started.empty()
    assert {thread: case.jobs._thread_current_job[(case.session_id, thread)] for thread in case.threads} == {
        thread: group[0] for thread, group in zip(case.threads, case.queued, strict=True)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["dispatch_projection", "task_start"])
@pytest.mark.parametrize("operation,terminal_write_fails,fail_queued", [
    ("cancel", False, False), ("cancel", True, False),
    ("force_interrupt", False, False), ("force_interrupt", True, False),
    ("force_interrupt", True, True),
])
async def test_cancel_unstarted_recovery_commits_terminal_and_wakes_original_fifo(
    pending_recovery_case: PendingRecoveryCase, monkeypatch: pytest.MonkeyPatch,
    failure: str, terminal_write_fails: bool, operation: str, fail_queued: bool,
) -> None:
    """派发硬错误后直接取消；冷 owner 必须看到取消，不能恢复成原请求。"""
    case = pending_recovery_case
    references = {item.job_id: item for item in await case.store.load(case.session_id)}
    original_save = case.jobs._pending_requests.refresh_version
    original_start = case.jobs._start_job_task
    saves = 0
    starts: list[str] = []

    async def fail_dispatch_projection(session_id: str) -> None:
        nonlocal saves
        saves += 1
        if failure == "dispatch_projection" and saves == 2:
            raise OSError("injected dispatch save failure")
        await original_save(session_id)

    def fail_task_start(job) -> None:
        if failure == "task_start" and job.job_id == case.queued[0][0]:
            raise OSError("injected task start failure")
        starts.append(job.job_id)
        original_start(job)

    monkeypatch.setattr(case.jobs._pending_requests, "refresh_version", fail_dispatch_projection)
    monkeypatch.setattr(case.jobs, "_start_job_task", fail_task_start)
    with pytest.raises(OSError, match="injected"):
        await case.jobs.list_pending(case.session_id)
    first, successor = case.queued[0]
    job = case.jobs._jobs[first]
    successor_job = case.jobs._jobs[successor]
    assert job.status == JobStatus.running and job.task is None
    assert starts == []
    writer = case.jobs._terminal_status_writer
    original_terminal = writer.mark_job_terminal_status
    terminal_failed = False

    def fail_terminal_once(**kwargs):
        nonlocal terminal_failed
        fault_target = case.queued[1][0] if fail_queued else first
        if terminal_write_fails and not terminal_failed and kwargs["job_id"] == fault_target:
            terminal_failed = True
            raise OSError("injected terminal write failure")
        return original_terminal(**kwargs)

    monkeypatch.setattr(writer, "mark_job_terminal_status", fail_terminal_once)

    async def cancel():
        if operation == "force_interrupt":
            with workspace_context(case.workspace_id, case.workspace_root):
                return await case.jobs.force_interrupt_active(reason="runtime_restart")
        return await case.jobs.control(first, JobControlRequest(action="cancel"))

    if terminal_write_fails:
        with pytest.raises(OSError, match="injected terminal write"):
            await cancel()
        assert job.status == JobStatus.cancelling
        assert case.jobs._thread_current_job[(case.session_id, case.threads[0])] == first
        assert case.jobs._input_owner.accepted_job_outcome(references[first]) == "unknown"
        assert starts == []
        if fail_queued:
            assert successor_job.status == JobStatus.cancelled
            events = await case.jobs._bus.list_events(successor)
            assert [event.type for event in events].count("job_cancelled") == 1
    result = await cancel()
    assert job.status == JobStatus.cancelled
    assert job.ended_at is not None
    assert case.jobs._jobs[successor] is successor_job
    if operation == "cancel":
        assert result.status == JobStatus.cancelled
        assert case.jobs._thread_current_job[(case.session_id, case.threads[0])] == successor
        assert starts == [successor]
        assert await asyncio.wait_for(case.executor.started.get(), 5) == successor
    else:
        assert result == (3 if fail_queued else 1 if terminal_write_fails else 4)
        assert all(item.status == JobStatus.cancelled for item in case.jobs._jobs.values())
        assert case.jobs._thread_current_job == {}
        assert starts == []
    events = await case.jobs._bus.list_events(first)
    assert [event.type for event in events].count("job_cancelled") == 1
    cold_saver = RolloutCheckpointSaver(sessions_dir=case.store._path_resolver.sessions_root)
    admission = cold_saver.execution_admission_for_job(
        case.session_id, thread_id=case.threads[0], job_id=first,
    )
    assert cold_saver.runtime_execution_outcome(
        case.session_id, case.threads[0], admission.execution_id,
    ) == "cancelled"
    remaining = await case.store.load(case.session_id)
    assert [item.job_id for item in remaining] == (list(case.queued[1]) if operation == "cancel" else [])
    if operation == "force_interrupt":
        for identity, reference in references.items():
            assert case.jobs._input_owner.accepted_job_outcome(reference) == "cancelled"
            events = await case.jobs._bus.list_events(identity)
            assert [event.type for event in events].count("job_cancelled") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel_paused", "force_paused", "cancel_active"])
async def test_live_cancel_write_failure_retains_owner_and_retry_uses_original_fifo(
    pending_recovery_case: PendingRecoveryCase, monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """真实准入和执行端口生成取消故障；失败不能变业务失败或释放原槽。"""
    case = pending_recovery_case
    references = {item.job_id: item for item in await case.store.load(case.session_id)}
    await case.jobs.list_pending(case.session_id)
    for group in case.queued:
        assert await asyncio.wait_for(case.executor.started.get(), 5) == group[0]
    first, successor = case.queued[0]
    job, successor_job = case.jobs._jobs[first], case.jobs._jobs[successor]
    original_task = job.task
    writer = case.jobs._terminal_status_writer
    original_terminal = writer.mark_job_terminal_status
    fault = OSError("injected live cancellation terminal failure")
    attempts: list[str] = []

    def fail_terminal_once(**kwargs):
        if kwargs["job_id"] == first:
            attempts.append(kwargs["status"])
            if len(attempts) == 1:
                raise fault
        return original_terminal(**kwargs)

    monkeypatch.setattr(writer, "mark_job_terminal_status", fail_terminal_once)
    if operation != "cancel_active":
        await case.jobs.control(first, JobControlRequest(action="pause"))
        assert not original_task.done()

    async def cancel():
        if operation == "force_paused":
            with workspace_context(case.workspace_id, case.workspace_root):
                return await case.jobs.force_interrupt_active(reason="runtime_restart")
        return await case.jobs.control(first, JobControlRequest(action="cancel"))

    if operation == "cancel_active":
        result = await cancel()
        assert result.status == JobStatus.cancelling
        with pytest.raises(OSError) as raised:
            await original_task
    else:
        with pytest.raises(OSError) as raised:
            await cancel()
    assert raised.value is fault
    assert attempts == ["cancelled"], "失败后不能在同一次取消中隐式重试"
    assert case.jobs._jobs[first] is job and job.task is original_task
    assert job.status == JobStatus.cancelling and job.ended_at is None
    assert case.jobs._thread_current_job[(case.session_id, case.threads[0])] == first
    assert case.jobs._input_owner.accepted_job_outcome(references[first]) == "unknown"
    events = await case.jobs._bus.list_events(first)
    assert not {"job_failed", "job_cancelled"}.intersection(event.type for event in events)
    assert case.executor.started.empty()
    if operation == "force_paused":
        assert all(other.status == JobStatus.cancelled for key, other in case.jobs._jobs.items() if key != first)
        assert await case.store.load(case.session_id) == []
    else:
        assert successor_job.status == JobStatus.queued
        # drain 必须暴露背景写入错误；先让仍正常运行的 sibling 通过产品暂停退出。
        sibling = case.jobs._jobs[case.queued[1][0]]
        await case.jobs.control(sibling.job_id, JobControlRequest(action="pause"))
    with pytest.raises(BaseExceptionGroup) as drained:
        await case.jobs.drain_finalizations()
    assert drained.value.exceptions == (fault,)

    result = await cancel()
    assert job.status == JobStatus.cancelled and job.ended_at is not None
    assert attempts == ["cancelled", "cancelled"]
    assert job.task is original_task and case.jobs._jobs[successor] is successor_job
    if operation == "force_paused":
        assert result == 1
        assert case.jobs._thread_current_job == {}
    else:
        assert result.status == JobStatus.cancelled
        assert case.jobs._thread_current_job[(case.session_id, case.threads[0])] == successor
        assert await asyncio.wait_for(case.executor.started.get(), 5) == successor
        assert case.executor.started.empty()
        await case.jobs.control(successor, JobControlRequest(action="pause"))
    await case.jobs.drain_finalizations()
    events = await case.jobs._bus.list_events(first)
    assert [event.type for event in events].count("job_cancelled") == 1
    assert not any(event.type == "job_failed" for event in events)
    cold_saver = RolloutCheckpointSaver(sessions_dir=case.store._path_resolver.sessions_root)
    admission = cold_saver.execution_admission_for_job(
        case.session_id, thread_id=case.threads[0], job_id=first,
    )
    assert cold_saver.runtime_execution_outcome(
        case.session_id, case.threads[0], admission.execution_id,
    ) == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_callback", [False, True])
async def test_prebody_cancel_write_failure_clears_only_after_original_job_retry(
    pending_recovery_case: PendingRecoveryCase, monkeypatch: pytest.MonkeyPatch,
    defer_callback: bool,
) -> None:
    case = pending_recovery_case
    first, successor = case.queued[0]
    original_run = case.jobs._run_job_background
    entered = asyncio.Event()

    async def wait_before_body(job_id, session_id, message):
        if job_id == first:
            entered.set()
            await asyncio.Future()
        await original_run(job_id, session_id, message)

    original_callback = case.jobs._on_finalization_done
    deferred: list[asyncio.Task] = []
    callback_ready = asyncio.Event()

    def defer_done(task, *, records_errors):
        if records_errors:
            deferred.append(task)
            callback_ready.set()
        else:
            original_callback(task, records_errors=records_errors)

    if defer_callback:
        monkeypatch.setattr(case.jobs, "_on_finalization_done", defer_done)
    monkeypatch.setattr(case.jobs, "_run_job_background", wait_before_body)
    await case.jobs.list_pending(case.session_id)
    await asyncio.wait_for(entered.wait(), 5)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == case.queued[1][0]
    sibling = case.jobs._jobs[case.queued[1][0]]
    await case.jobs.control(sibling.job_id, JobControlRequest(action="pause"))
    job = case.jobs._jobs[first]
    original_task = job.task
    writer = case.jobs._terminal_status_writer
    original_terminal = writer.mark_job_terminal_status
    fault = OSError("injected prebody cancellation terminal failure")
    attempts = 0

    def fail_terminal_once(**kwargs):
        nonlocal attempts
        if kwargs["job_id"] == first:
            attempts += 1
            if attempts == 1:
                raise fault
        return original_terminal(**kwargs)

    monkeypatch.setattr(writer, "mark_job_terminal_status", fail_terminal_once)
    await case.jobs.control(first, JobControlRequest(action="cancel"))
    if defer_callback:
        await asyncio.wait_for(callback_ready.wait(), 5)
        assert case.jobs._finalization_errors[first] is fault
    else:
        with pytest.raises(BaseExceptionGroup) as raised:
            await case.jobs.drain_finalizations()
        assert raised.value.exceptions == (fault,)
    assert job.status == JobStatus.cancelling and job.task is original_task
    assert case.jobs._thread_current_job[(case.session_id, case.threads[0])] == first
    assert case.jobs._jobs[successor].status == JobStatus.queued
    assert case.executor.started.empty()
    events = await case.jobs._bus.list_events(first)
    assert not {"job_failed", "job_cancelled"}.intersection(event.type for event in events)

    result = await case.jobs.control(first, JobControlRequest(action="cancel"))
    assert result.status == JobStatus.cancelled and attempts == 2
    assert job.task is original_task
    if defer_callback:
        assert len(deferred) == 1
        original_callback(deferred[0], records_errors=True)
    assert await asyncio.wait_for(case.executor.started.get(), 5) == successor
    await case.jobs.control(successor, JobControlRequest(action="pause"))
    await case.jobs.drain_finalizations()
    events = await case.jobs._bus.list_events(first)
    assert [event.type for event in events].count("job_cancelled") == 1
    assert not any(event.type == "job_failed" for event in events)
