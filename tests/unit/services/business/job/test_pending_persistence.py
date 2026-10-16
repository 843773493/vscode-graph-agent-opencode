import asyncio
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from app.core.job_event_bus import JobEventBus
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.pending_request import (
    PendingRequestControl,
    PendingRequestDTO,
)
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
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
        return PendingRequestStore(sessions_dir=sessions_dir, input_reader=lambda reference: pending_input_owner.accepted_job_message(reference, display=False))
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
        pending_request_store=PendingRequestStore(sessions_dir=sessions_dir, input_reader=lambda reference: input_owner.accepted_job_message(reference, display=False)),
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
    ), execution_identity=execution_binding)
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
    store = store_factory(sessions_dir)
    await store.save(
        "ses_019c018e721e70b38f769ca61010e13a",
        [
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
        ],
    )

    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard, input_owner=pending_input_owner)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    restored = await service.list_pending("ses_019c018e721e70b38f769ca61010e13a")

    assert restored.active_job_id == "job_first"
    assert [item.message_id for item in restored.requests] == ["msg_second"]
    assert restored.requests[0].status == "queued"
    assert started_jobs == ["job_first"]


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
    store = store_factory(sessions_dir)
    await store.save(
        "ses_019b8605a35b7fd6829e9dfec660d38c",
        [
            request_factory(
                "ses_019b8605a35b7fd6829e9dfec660d38c",
                job_id="job_restored_first",
                message_id="msg_restored_first",
                sequence=1,
            )
        ],
    )
    service = _service(sessions_dir, session_lifecycle_guard=session_lifecycle_guard, input_owner=pending_input_owner)
    started_jobs: list[str] = []
    _prevent_background_execution(service, monkeypatch, started_jobs)

    new_binding = service.reserve_user_execution_identity()
    new_created = datetime.now(UTC)
    new_thread = pending_input_owner._resolve_main_thread_id("ses_019b8605a35b7fd6829e9dfec660d38c")
    pending_input_owner.accept_user_message(MessageDTO(
        message_id="msg_new", session_id="ses_019b8605a35b7fd6829e9dfec660d38c", thread_id=new_thread,
        role=MessageRole.user, content="后发送", created_at=new_created, updated_at=new_created,
    ), execution_identity=new_binding)
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
    assert service._pending_queue.ids("ses_019b8605a35b7fd6829e9dfec660d38c") == (
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
    await store.save(
        session_id,
        [
            request_factory(
                session_id,
                job_id="job_started",
                message_id="msg_started",
                sequence=1,
            ),
        ],
    )

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
    store = store_factory(sessions_dir)
    await store.save(
        session_id,
        [
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
        ],
    )

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
    store = store_factory(sessions_dir)
    await store.save(
        session_id,
        [
            request_factory(
                session_id,
                job_id="job_head",
                message_id="msg_head",
                sequence=1,
                request_id="request_restored1234",
            ),
        ],
    )

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
    await store.save(session_id, [cancelled, successor])
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
