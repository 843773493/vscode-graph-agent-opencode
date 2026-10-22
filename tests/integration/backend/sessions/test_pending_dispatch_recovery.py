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
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.common import MessageRole
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


@pytest.fixture
async def pending_recovery_case(request: pytest.FixtureRequest) -> AsyncIterator[PendingRecoveryCase]:
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
    child = await factory.for_owner_session(session_id).create(
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
        input_reader=lambda reference: messages.accepted_job_message(reference, display=False),
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
                messages.accept_user_message(message, execution_identity=identity)
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
        assert [item.job_id for item in await store.load(session_id)] == [job for group in queued for job in group]
        yield PendingRecoveryCase(session_id, threads, tuple(queued), cold, cold_executor, store)
    finally:
        await _stop_jobs(source)
        await _stop_jobs(cold)
        for service in factory._services.values():
            service.control_store.close()
        workspace.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["load_save", "first_dispatch_save", "sibling_dispatch_save", "sibling_task"])
async def test_real_pending_queue_retries_without_replacing_or_replaying_sibling(
    pending_recovery_case: PendingRecoveryCase, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    case = pending_recovery_case
    original_save = case.jobs._pending_requests.persist_current
    original_start = case.jobs._start_job_task
    save_calls = 0
    starts: list[str] = []
    failing_save = {"load_save": 1, "first_dispatch_save": 2, "sibling_dispatch_save": 3}.get(failure)
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

    monkeypatch.setattr(case.jobs._pending_requests, "persist_current", save_once)
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
