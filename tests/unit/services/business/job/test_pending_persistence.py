import asyncio
import threading
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from app.core.job_event_bus import JobEventBus
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.common import JobStatus, MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.pending_request import (
    PendingRequestControl,
    PendingRequestDTO,
)
from app.services.business.job.service import (
    JobAdmissionClosedError,
    JobService,
)
from app.services.business.message_service import MessageService
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.job_thread_fixture import (
    job_fixture_thread_id,
)
from tests.support.message_service import build_message_service


@pytest.fixture
def pending_input_owner(tmp_path) -> MessageService:
    sessions_dir = tmp_path / "sessions"
    return build_message_service(sessions_dir, checkpointer=RolloutCheckpointSaver(sessions_dir=sessions_dir), workspace_root=tmp_path)


@pytest.fixture
def request_factory(pending_input_owner):
    return partial(_request, input_owner=pending_input_owner)


@pytest.fixture
def store_factory(pending_input_owner):
    def make(sessions_dir):
        return PendingRequestStore(sessions_dir=sessions_dir, owner=pending_input_owner._checkpointer, input_reader=lambda reference: pending_input_owner.accepted_job_message(reference, display=False))
    return make


class _UnusedExecutor:
    async def run(self, job):
        raise AssertionError(f"恢复待处理消息不应执行 Job: {job.job_id}")


class _PendingTask:
    def done(self) -> bool:
        return False

    def add_done_callback(self, _callback) -> None:
        return None


def _service(sessions_dir: Path, session_lifecycle_guard, input_owner: MessageService) -> JobService:
    return JobService(
        job_event_bus=JobEventBus(),
        job_executor=_UnusedExecutor(),
        pending_request_store=PendingRequestStore(sessions_dir=sessions_dir, owner=input_owner._checkpointer, input_reader=lambda reference: input_owner.accepted_job_message(reference, display=False)),
        session_lifecycle_guard=session_lifecycle_guard,
        input_owner=input_owner,
        terminal_status_writer=input_owner._checkpointer,
    )


def _prevent_background_execution(
    service: JobService,
    monkeypatch: pytest.MonkeyPatch,
    started_jobs: list[str] | None = None,
) -> None:
    def fake_start(job) -> None:
        if started_jobs is not None:
            started_jobs.append(job.job_id)
        job.task = _PendingTask()

    monkeypatch.setattr(service, "_start_job_task", fake_start)


async def _assert_delete_waits_for_preparation(service: JobService, session_id: str) -> None:
    """取消中的恢复仍保护原存储窗口；删除可排队，不能提前触碰存储。"""
    entered = asyncio.Event()

    async def delete_storage() -> None:
        entered.set()

    deletion = asyncio.create_task(service.run_session_delete_operation(session_id, delete_storage))
    try:
        async def closed() -> None:
            while session_id not in service._deleting_sessions:
                if deletion.done():
                    await deletion
                await asyncio.sleep(0)

        await asyncio.wait_for(closed(), timeout=3)
        assert not deletion.done()
        assert not entered.is_set()
        assert service._session_preparations[session_id] > 0
    finally:
        deletion.cancel()
        await asyncio.gather(deletion, return_exceptions=True)
    assert session_id not in service._deleting_sessions


def _request(
    session_id: str,
    *,
    input_owner: MessageService,
    job_id: str,
    message_id: str,
    sequence: int,
    gateway_id: str | None = "gateway_test",
    request_id: str | None = "request_test",
) -> PendingRequestDTO:
    now = datetime.now(UTC)
    execution_binding = UserExecutionIdentity(
        job_id=job_id,
        turn_id=f"turn_{job_id}",
        admission_idempotency_key=f"admission_{job_id}",
    )
    request = PendingRequestDTO(
        job_id=job_id,
        execution_binding=execution_binding,
        message_id=message_id,
        session_id=session_id,
        thread_id=input_owner._resolve_main_thread_id(session_id),
        turn_id=execution_binding.turn_id,
        content=message_id,
        delivery_policy="after_turn",
        enqueue_sequence=sequence,
        position=sequence - 1,
        agent_id="default",
        message_created_at=now.isoformat(),
        created_at=now,
        updated_at=now,
        snapshot_version=1,
        gateway_id=gateway_id,
        request_id=request_id,
    )
    input_owner.accept_user_message(MessageDTO(
        message_id=message_id, session_id=session_id, thread_id=request.thread_id,
        role=MessageRole.user, content=message_id, created_at=now, updated_at=now,
    ), execution_identity=execution_binding, queue_input=ExecutionQueueInput(
        execution_binding=execution_binding, message_id=message_id, agent_id="default", provider_id=None,
        delivery_policy="after_turn", gateway_id=gateway_id, request_id=request_id, created_at=now.isoformat(),
    ))
    reference = PendingRequestControl.model_validate(request.model_dump(exclude={"content", "attachments", "message_created_at", "message_metadata"}))
    message = input_owner.accepted_job_message(reference, display=False)
    return request.model_copy(update={"message_created_at": message.created_at.isoformat(), "message_metadata": {**message.metadata, "boxteam_session_provider_id": None}})


@pytest.mark.asyncio
async def test_job_service_restores_only_messages_still_in_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
    session_lifecycle_guard,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "ses_019c018e721e70b38f769ca61010e13a")
    _ = [
            request_factory(
                "ses_019c018e721e70b38f769ca61010e13a",
                job_id="job_first",
                message_id="msg_first",
                sequence=1,
            ),
            request_factory(
                "ses_019c018e721e70b38f769ca61010e13a",
                job_id="job_second",
                message_id="msg_second",
                sequence=2,
            ),
        ]

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard, input_owner=pending_input_owner)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    restored = await service.list_pending("ses_019c018e721e70b38f769ca61010e13a")

    assert restored.active_job_id == "job_first"
    assert [item.message_id for item in restored.requests] == ["msg_second"]
    assert restored.requests[0].status == "queued"
    assert started_jobs == ["job_first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["list_pending", "start_restored_executions", "notify_boundary"])
async def test_cancelled_pending_restore_settles_worker_before_session_delete(
    entrypoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory,
    store_factory,
    pending_input_owner,
    session_lifecycle_guard,
) -> None:
    session_id = "ses_019c018e721e70b38f769ca61010e13a"
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    _ = [request_factory(session_id, job_id="job_restore", message_id="msg_restore", sequence=1)]
    service = _service(sessions_dir, session_lifecycle_guard, pending_input_owner)
    async def do_nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_start_next_pending", do_nothing)
    started = threading.Event()
    release = threading.Event()
    original = pending_input_owner.accepted_job_outcome

    def blocked_outcome(reference):
        started.set()
        if not release.wait(timeout=10):
            raise TimeoutError("测试未释放 pending restore worker")
        return original(reference)

    monkeypatch.setattr(pending_input_owner, "accepted_job_outcome", blocked_outcome)
    if entrypoint == "list_pending":
        operation = service.list_pending(session_id)
    elif entrypoint == "start_restored_executions":
        operation = service.start_restored_executions()
    else:
        operation = service.notify_boundary(
            session_id,
            "after_tool_result",
            thread_id=job_fixture_thread_id(session_id),
        )
    restore = asyncio.create_task(operation)
    assert await asyncio.to_thread(started.wait, 5)
    restore.cancel()

    try:
        await _assert_delete_waits_for_preparation(service, session_id)
        assert not restore.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await restore
    restored = await service.list_pending(session_id)
    assert [item.job_id for item in restored.requests] == ["job_restore"]
    assert [item.job_id for item in (await service.list_pending(session_id)).requests] == ["job_restore"]
    assert await service.run_session_delete_operation(session_id, lambda: asyncio.sleep(0, result="deleted")) == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_stage", [1, 2])
async def test_cancelled_restore_finishes_queue_persistence_and_launch(
    persist_stage: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory,
    store_factory,
    pending_input_owner,
    session_lifecycle_guard,
) -> None:
    session_id = "ses_019c018e721e70b38f769ca61010e13a"
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    _ = [request_factory(
        session_id, job_id="job_restore", message_id="msg_restore", sequence=1,
    )]
    service = _service(sessions_dir, session_lifecycle_guard, pending_input_owner)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)
    reached = asyncio.Event()
    release = asyncio.Event()
    original = service._pending_requests.refresh_version
    calls = 0

    async def blocked_persist(target_session_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == persist_stage:
            reached.set()
            await release.wait()
        await original(target_session_id)

    monkeypatch.setattr(service._pending_requests, "refresh_version", blocked_persist)
    restore = asyncio.create_task(service.list_pending(session_id))
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        restore.cancel()
        await asyncio.sleep(0)
        restore.cancel()
        await _assert_delete_waits_for_preparation(service, session_id)
        assert not restore.done()
        assert started_jobs == []
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await restore
    snapshot = await service.list_pending(session_id)
    assert snapshot.active_job_id == "job_restore"
    assert snapshot.requests == []
    assert started_jobs == ["job_restore"]
    assert await store.load(session_id) == []


@pytest.mark.asyncio
async def test_failed_canonical_restore_keeps_pending_available_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory,
    store_factory,
    pending_input_owner,
    session_lifecycle_guard,
) -> None:
    session_id = "ses_019c018e721e70b38f769ca61010e13a"
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    _ = [request_factory(
        session_id, job_id="job_restore", message_id="msg_restore", sequence=1,
    )]
    service = _service(sessions_dir, session_lifecycle_guard, pending_input_owner)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)
    original = pending_input_owner.accepted_job_outcome
    fail = True

    def fail_once(reference):
        nonlocal fail
        if fail:
            fail = False
            raise OSError("canonical outcome 读取失败")
        return original(reference)

    monkeypatch.setattr(pending_input_owner, "accepted_job_outcome", fail_once)
    with pytest.raises(OSError, match="canonical outcome 读取失败"):
        await service.list_pending(session_id)
    assert started_jobs == []
    assert [item.job_id for item in await store.load(session_id)] == ["job_restore"]
    restored = await service.list_pending(session_id)
    assert restored.active_job_id == "job_restore"
    assert restored.requests == []
    assert started_jobs == ["job_restore"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["start_restored_executions", "notify_boundary"])
async def test_closed_admission_allows_existing_pending_restore_but_rejects_new_requests(
    entrypoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory,
    store_factory,
    pending_input_owner,
    session_lifecycle_guard,
) -> None:
    session_id = "ses_019c018e721e70b38f769ca61010e13a"
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    _ = [request_factory(session_id, job_id="job_restore", message_id="msg_restore", sequence=1)]
    service = _service(sessions_dir, session_lifecycle_guard, pending_input_owner)
    thread_id = pending_input_owner._resolve_main_thread_id(session_id)

    async def do_nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_start_next_pending", do_nothing)
    service.close_admission()

    if entrypoint == "start_restored_executions":
        assert await service.start_restored_executions() == 0
        assert service._jobs["job_restore"].status == JobStatus.queued
    else:
        snapshot = await service.notify_boundary(
            session_id,
            "after_tool_result",
            thread_id=thread_id,
        )
        assert [item.job_id for item in snapshot.requests] == ["job_restore"]

    with pytest.raises(JobAdmissionClosedError):
        await service.list_pending(session_id)
    with pytest.raises(JobAdmissionClosedError):
        await service.start_job(
            session_id,
            "new message",
            thread_id=thread_id,
            message_id="msg_new",
            message_created_at="2026-07-17T00:00:01+00:00",
        )


@pytest.mark.asyncio
async def test_restore_and_new_send_keep_one_session_fifo_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
    session_lifecycle_guard,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "ses_019b8605a35b7fd6829e9dfec660d38c")
    _ = [
            request_factory(
                "ses_019b8605a35b7fd6829e9dfec660d38c",
                job_id="job_restored_first",
                message_id="msg_restored_first",
                sequence=1,
            )
        ]
    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard, input_owner=pending_input_owner)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    new_binding = service.reserve_user_execution_identity()
    new_created = datetime.now(UTC)
    new_thread = pending_input_owner._resolve_main_thread_id("ses_019b8605a35b7fd6829e9dfec660d38c")
    pending_input_owner.accept_user_message(MessageDTO(
        message_id="msg_new", session_id="ses_019b8605a35b7fd6829e9dfec660d38c", thread_id=new_thread,
        role=MessageRole.user, content="后发送", created_at=new_created, updated_at=new_created,
    ), execution_identity=new_binding, queue_input=ExecutionQueueInput(
        execution_binding=new_binding, message_id="msg_new", agent_id="default", provider_id=None,
        delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=new_created.isoformat(),
    ))
    _snapshot, new_dispatch = await asyncio.gather(
        service.list_pending("ses_019b8605a35b7fd6829e9dfec660d38c"),
        service.start_job(
            "ses_019b8605a35b7fd6829e9dfec660d38c",
            "后发送",
            thread_id=pending_input_owner._resolve_main_thread_id("ses_019b8605a35b7fd6829e9dfec660d38c"),
            message_id="msg_new",
            message_created_at=new_created.isoformat(),
            execution_binding=new_binding,
        ),
    )

    assert started_jobs == ["job_restored_first"]
    assert new_dispatch.job_status == "queued"
    assert (
        service._thread_current_job[("ses_019b8605a35b7fd6829e9dfec660d38c", pending_input_owner._resolve_main_thread_id("ses_019b8605a35b7fd6829e9dfec660d38c"))]
        == "job_restored_first"
    )
    assert service._pending_queue.queued_job_ids("ses_019b8605a35b7fd6829e9dfec660d38c") == (
        new_dispatch.job_id,
    )


@pytest.mark.asyncio
async def test_dispatch_removes_started_head_from_persistent_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
    session_lifecycle_guard,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c5edf70037e1791483733a8865f97"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    _ = [
            request_factory(
                session_id,
                job_id="job_started",
                message_id="msg_started",
                sequence=1,
            ),
        ]

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard, input_owner=pending_input_owner)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    pending = await service.list_pending(session_id)

    assert pending.active_job_id == "job_started"
    assert pending.requests == []
    assert started_jobs == ["job_started"]
    assert await store.load(session_id) == []


@pytest.mark.asyncio
async def test_restored_pending_job_carries_persisted_gateway_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
    session_lifecycle_guard,
) -> None:
    """磁盘恢复的待处理 Job 必须沿用创建时持久化的真实 gateway_id。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b990a9cce734bbccc60ec5321f320"
    session_bundle_factory(sessions_dir, session_id)
    _ = [
            request_factory(
                session_id,
                job_id="job_head",
                message_id="msg_head",
                sequence=1,
                gateway_id="gateway_restored1234",
            ),
            request_factory(
                session_id,
                job_id="job_tail",
                message_id="msg_tail",
                sequence=2,
                gateway_id="gateway_restored1234",
            ),
        ]

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard, input_owner=pending_input_owner)
    _prevent_background_execution(service, monkeypatch)

    restored = await service.list_pending(session_id)

    assert restored.active_job_id == "job_head"
    assert service._jobs["job_head"].gateway_id == "gateway_restored1234"
    assert service._jobs["job_tail"].gateway_id == "gateway_restored1234"


@pytest.mark.asyncio
async def test_restored_pending_job_carries_persisted_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
    session_lifecycle_guard,
) -> None:
    """磁盘恢复的待处理 Job 必须沿用创建请求的权威 request_id，不补造第二个。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_00000000f00070008000000000a1b2c3"
    session_bundle_factory(sessions_dir, session_id)
    _ = [
            request_factory(
                session_id,
                job_id="job_head",
                message_id="msg_head",
                sequence=1,
                request_id="request_restored1234",
            ),
        ]

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard, input_owner=pending_input_owner)
    _prevent_background_execution(service, monkeypatch)

    restored = await service.list_pending(session_id)

    assert restored.active_job_id == "job_head"
    assert service._jobs["job_head"].request_id == "request_restored1234"


@pytest.mark.asyncio
async def test_restore_discards_reference_after_canonical_cancel_before_queue_save(
    tmp_path, monkeypatch, session_bundle_factory, session_lifecycle_guard,
    request_factory, store_factory, pending_input_owner,
):
    """模拟终态先落盘、调度文件尚未删除引用时进程退出。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b990a9cce734bbccc60ec5321f320"
    session_bundle_factory(sessions_dir, session_id)
    cancelled = request_factory(session_id, job_id="job_cancelled", message_id="msg_cancelled", sequence=1)
    successor = request_factory(session_id, job_id="job_successor", message_id="msg_successor", sequence=2)
    store = store_factory(sessions_dir)
    _ = [cancelled, successor]
    assert pending_input_owner._checkpointer.mark_job_terminal_status(
        session_id=session_id, thread_id=cancelled.thread_id, job_id=cancelled.job_id, status="cancelled",
    )
    restarted_owner = build_message_service(sessions_dir, checkpointer=RolloutCheckpointSaver(sessions_dir=sessions_dir), workspace_root=tmp_path)
    service = _service(sessions_dir, session_lifecycle_guard, restarted_owner)
    started = []
    _prevent_background_execution(service, monkeypatch, started)
    snapshot = await service.list_pending(session_id)
    assert started == [successor.job_id]
    assert service._jobs[successor.job_id].message == successor.content
    assert snapshot.active_job_id == successor.job_id and snapshot.requests == []
    assert await store.load(session_id) == []
    assert restarted_owner._checkpointer.accepted_user_message(
        session_id, thread_id=cancelled.thread_id, job_id=cancelled.job_id, message_id=cancelled.message_id,
    ).content == cancelled.content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["load_projection", "dispatch_projection", "task_start"])
async def test_restore_retries_hard_error_without_reloading_fifo(
    failure, tmp_path, monkeypatch, session_bundle_factory,
    request_factory, store_factory, pending_input_owner, session_lifecycle_guard,
) -> None:
    session_id = "ses_019c018e721e70b38f769ca61010e13a"
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    records = [request_factory(
        session_id, job_id=f"job_{index}", message_id=f"msg_{index}", sequence=index,
    ) for index in (1, 2)]
    _ = records
    service = _service(sessions_dir, session_lifecycle_guard, pending_input_owner)
    started: list[str] = []
    _prevent_background_execution(service, monkeypatch, started)
    original_save = service._pending_requests.refresh_version
    original_start = service._start_job_task
    save_calls = 0

    async def fail_save_once(target_session_id):
        nonlocal save_calls
        save_calls += 1
        if (failure, save_calls) in {("load_projection", 1), ("dispatch_projection", 2)}:
            raise OSError("injected pending save failure")
        await original_save(target_session_id)

    def fail_start_once(job):
        monkeypatch.setattr(service, "_start_job_task", original_start)
        raise OSError("injected pending task failure")

    monkeypatch.setattr(service._pending_requests, "refresh_version", fail_save_once)
    if failure == "task_start":
        monkeypatch.setattr(service, "_start_job_task", fail_start_once)
    with pytest.raises(OSError, match="injected pending"):
        await service.list_pending(session_id)
    original_jobs = dict(service._jobs)
    assert started == []
    restored = await service.list_pending(session_id)
    assert restored.active_job_id == records[0].job_id
    assert [item.job_id for item in restored.requests] == [records[1].job_id]
    assert started == [records[0].job_id]
    assert all(service._jobs[key] is job for key, job in original_jobs.items())
    assert service._jobs[records[0].job_id].execution_binding == records[0].execution_binding
    assert [item.message_id for item in await store.load(session_id)] == [records[1].message_id]
    save_count = save_calls
    assert (await service.list_pending(session_id)).active_job_id == records[0].job_id
    assert started == [records[0].job_id]
    assert save_calls > save_count
    assert service._jobs[records[0].job_id].queue_record.claim_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["save", "task_start"])
async def test_loaded_dispatch_retries_original_active_slot_once(
    failure, tmp_path, monkeypatch, session_bundle_factory,
    request_factory, store_factory, pending_input_owner, session_lifecycle_guard,
) -> None:
    from app.services.business.job.lifecycle import transition_job_status

    session_id = "ses_019c018e721e70b38f769ca61010e13a"
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    records = [request_factory(
        session_id, job_id=f"job_{index}", message_id=f"msg_{index}", sequence=index,
    ) for index in (1, 2, 3)]
    _ = records
    service = _service(sessions_dir, session_lifecycle_guard, pending_input_owner)
    started: list[str] = []
    _prevent_background_execution(service, monkeypatch, started)
    await service.list_pending(session_id)
    original_jobs = dict(service._jobs)
    first_job = service._jobs[records[0].job_id]
    assert pending_input_owner._checkpointer.mark_job_terminal_status(
        session_id=session_id, thread_id=first_job.thread_id, job_id=first_job.job_id, status="cancelled")
    transition_job_status(first_job, JobStatus.cancelled)
    original_save = service._pending_requests.refresh_version
    original_start = service._start_job_task

    async def fail_save_once(target_session_id):
        monkeypatch.setattr(service._pending_requests, "refresh_version", original_save)
        raise OSError("injected pending save failure")

    def fail_start_once(job):
        monkeypatch.setattr(service, "_start_job_task", original_start)
        raise OSError("injected pending task failure")

    if failure == "save":
        monkeypatch.setattr(service._pending_requests, "refresh_version", fail_save_once)
    else:
        monkeypatch.setattr(service, "_start_job_task", fail_start_once)
    with pytest.raises(OSError, match="injected pending"):
        await service._schedule_next_job_if_needed(first_job)
    assert started == [records[0].job_id]
    results = await asyncio.gather(*(
        service.notify_boundary(session_id, "after_turn", thread_id=records[0].thread_id)
        for _ in range(3)
    ))
    assert all(result.active_job_id == records[1].job_id for result in results)
    assert all([item.job_id for item in result.requests] == [records[2].job_id] for result in results)
    assert started == [records[0].job_id, records[1].job_id]
    assert all(service._jobs[key] is job for key, job in original_jobs.items())
    assert [item.message_id for item in await store.load(session_id)] == [records[2].message_id]
