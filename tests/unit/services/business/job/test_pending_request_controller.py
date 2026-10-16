from datetime import UTC, datetime

import pytest

from app.core.job_event_bus import JobEventBus
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.prompting import PromptSection, internal_message_factory
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.job.service import JobService
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from tests.support.message_service import build_message_service
from tests.unit.services.business.job.test_pending_persistence import (
    pending_input_owner as pending_input_owner,  # noqa: PLC0414
)
from tests.unit.services.business.job.test_pending_persistence import (
    store_factory as store_factory,  # noqa: PLC0414
)


class _DummyJobExecutor:
    async def run(self, job):
        return "ok"


class _DummyTask:
    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        return None


def _service(monkeypatch: pytest.MonkeyPatch, session_lifecycle_guard) -> JobService:
    service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=_DummyJobExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )
    monkeypatch.setattr(
        service,
        "_start_job_task",
        lambda job: setattr(job, "task", _DummyTask()),
    )
    return service


@pytest.mark.asyncio
async def test_thread_pending_reads_keep_exact_active_slot_and_session_controls(
    monkeypatch: pytest.MonkeyPatch, session_lifecycle_guard,
) -> None:
    service = _service(monkeypatch, session_lifecycle_guard)
    session_id = "session_scoped_pending"
    main_thread = "thr_main_pending"
    child_thread = "thr_child_pending"
    jobs = {}
    # child 先入槽；Session 首 slot 不能被 main 读取误用。
    for thread_id, label in [(child_thread, "child"), (main_thread, "main")]:
        for suffix in ["active", "queued"]:
            message_id = f"msg_{label}_{suffix}"
            jobs[message_id] = await service.start_job(
                session_id, message_id, thread_id=thread_id, message_id=message_id,
                message_created_at="2026-07-17T00:00:00+00:00",
            )

    aggregate = await service.list_pending(session_id)
    assert aggregate.thread_id is None
    assert aggregate.active_job_id == jobs["msg_child_active"].job_id
    assert [item.message_id for item in aggregate.requests] == ["msg_child_queued", "msg_main_queued"]

    main = await service.list_pending(session_id, thread_id=main_thread)
    assert main.thread_id == main_thread
    assert main.active_job_id == jobs["msg_main_active"].job_id
    assert [(item.message_id, item.position) for item in main.requests] == [("msg_main_queued", 0)]
    summaries = await service.list_pending_summaries(session_id, thread_id=main_thread, limit=1)
    assert summaries.thread_id == main_thread
    assert summaries.active_job_id == main.active_job_id
    assert summaries.request_count == 1 and summaries.truncated is False
    assert [(item.message_id, item.thread_id) for item in summaries.requests] == [("msg_main_queued", main_thread)]

    # Session 变更仍控制全体 thread，返回聚合快照。
    changed = await service.update_pending_policy(
        session_id, "msg_child_queued", delivery_policy="after_tool_result",
        expected_snapshot_version=main.snapshot_version,
    )
    assert changed.thread_id is None
    assert [(item.message_id, item.delivery_policy) for item in changed.requests] == [
        ("msg_child_queued", "after_tool_result"), ("msg_main_queued", "after_turn"),
    ]


@pytest.mark.asyncio
async def test_pending_controls_policy_remove_without_reordering(
    monkeypatch, session_lifecycle_guard, tmp_path, session_bundle_factory,
    pending_input_owner, store_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b990a9cce734bbccc60ec5321f320"
    session_bundle_factory(sessions_dir, session_id)
    saver = pending_input_owner._checkpointer
    thread_id = pending_input_owner._resolve_main_thread_id(session_id)
    store = store_factory(sessions_dir)
    service = JobService(
        job_event_bus=JobEventBus(), job_executor=_DummyJobExecutor(),
        session_lifecycle_guard=session_lifecycle_guard, pending_request_store=store,
        input_owner=pending_input_owner, terminal_status_writer=saver,
    )
    monkeypatch.setattr(service, "_start_job_task", lambda job: setattr(job, "task", _DummyTask()))

    async def accepted(content, message_id, delivery_policy="after_turn"):
        binding = service.reserve_user_execution_identity()
        now = datetime.now(UTC)
        pending_input_owner.accept_user_message(MessageDTO(
            message_id=message_id, session_id=session_id, thread_id=thread_id, role=MessageRole.user,
            content=content, created_at=now, updated_at=now,
        ), execution_identity=binding)
        return await service.start_job(
            session_id, content, thread_id=thread_id, message_id=message_id,
            message_created_at=now.isoformat(), execution_binding=binding, delivery_policy=delivery_policy,
        )

    active = await accepted("active", "msg_active")
    queued = await accepted("queued", "msg_queued")
    tail = await accepted("tail", "msg_tail", "after_interrupt")
    snapshot = await service.list_pending(session_id)
    assert [item.message_id for item in snapshot.requests] == ["msg_queued", "msg_tail"]
    assert [item.enqueue_sequence for item in snapshot.requests] == [2, 3]
    assert all(item.status == "queued" for item in snapshot.requests)
    changed = await service.update_pending_policy(
        session_id, "msg_tail", delivery_policy="after_tool_result",
        expected_snapshot_version=snapshot.snapshot_version,
    )
    assert [(item.message_id, item.content, item.delivery_policy) for item in changed.requests] == [
        ("msg_queued", "queued", "after_turn"), ("msg_tail", "tail", "after_tool_result"),
    ]
    with pytest.raises(ValueError, match="不支持重排"):
        await service.reject_pending_reorder(session_id)
    with saver._storage.storage.read_connection(session_id, thread_id, "") as connection:
        original = connection.execute("SELECT turn_id,turn_ordinal,root_input_item_id FROM turn_records ORDER BY turn_ordinal").fetchall()
    removed = await service.remove_pending(session_id, "msg_queued")
    assert [item.message_id for item in removed.requests] == ["msg_tail"]
    assert [item.message_id for item in await store.load(session_id)] == ["msg_tail"]
    extra = await accepted("also queued", "msg_extra")
    assert len((await service.list_pending(session_id)).requests) == 2
    cleared = await service.clear_pending(session_id)
    assert cleared.requests == [] and cleared.active_job_id == active.job_id
    assert await store.load(session_id) == []
    cancelled_jobs = {queued.job_id, tail.job_id, extra.job_id}
    with saver._storage.storage.read_connection(session_id, thread_id, "") as connection:
        assert connection.execute("SELECT turn_id,turn_ordinal,root_input_item_id FROM turn_records ORDER BY turn_ordinal").fetchall()[:3] == original
        assert {row[0] for row in connection.execute("SELECT job_id FROM executions WHERE outcome='cancelled'")} == cancelled_jobs
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone() == (0,)
    # 冷 owner 读取同一 cancelled 历史，重启队列不会再次派发已撤回输入。
    with RolloutCheckpointSaver(sessions_dir=sessions_dir) as restarted:
        owner = build_message_service(sessions_dir, checkpointer=restarted, workspace_root=tmp_path)
        restored = JobService(
            job_event_bus=JobEventBus(), job_executor=_DummyJobExecutor(),
            session_lifecycle_guard=session_lifecycle_guard, pending_request_store=store,
            input_owner=owner, terminal_status_writer=restarted,
        )
        assert (await restored.list_pending(session_id)).requests == [] and restored._jobs == {}
        history = RolloutHistoryReader(RolloutContextReader(restarted._storage)).load(
            session_id, thread_id, TurnHistoryLoadRequest(direction="tail", turns=4),
        )
        cancelled = [turn for turn in history.items if turn.status == "cancelled"]
        assert [turn.user_messages[0].content for turn in cancelled] == ["queued", "tail", "also queued"]


@pytest.mark.asyncio
async def test_job_service_returns_frozen_execution_binding(
    monkeypatch, session_lifecycle_guard
):
    service = _service(monkeypatch, session_lifecycle_guard=session_lifecycle_guard)
    user_job = await service.start_job(
        "session_execution_binding",
        "user turn",
        thread_id="thr_session_execution_binding",
        message_id="msg_user_binding",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    user_binding = service.get_execution_binding(user_job.job_id)
    assert isinstance(user_binding, UserExecutionIdentity)
    assert user_binding.job_id == user_job.job_id
    assert user_binding.turn_id
    assert user_binding.admission_idempotency_key
    assert (
        len(
            {
                user_binding.job_id,
                user_binding.turn_id,
                user_binding.admission_idempotency_key,
            }
        )
        == 3
    )

    internal_binding = InternalExecutionIntent(
        message_id="msg_internal_binding",
        execution_id="intr_execution_binding",
        admission_idempotency_key="adm_execution_binding",
    )
    internal_job = await service.start_job(
        "session_execution_binding",
        "internal message",
        thread_id="thr_session_execution_binding",
        execution_binding=internal_binding,
        message_id=internal_binding.message_id,
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    assert service.get_execution_binding(internal_job.job_id) is internal_binding

    with pytest.raises(KeyError, match="execution binding 不存在"):
        service.get_execution_binding("job_missing")


@pytest.mark.asyncio
async def test_internal_job_allocates_one_id_when_reservation_is_absent(
    monkeypatch, session_lifecycle_guard
):
    service = _service(monkeypatch, session_lifecycle_guard=session_lifecycle_guard)
    reservations: list[str] = []
    monkeypatch.setattr(
        service,
        "reserve_internal_job_id",
        lambda: reservations.append("allocated") or "job_allocated_once",
    )
    execution_binding = InternalExecutionIntent(
        message_id="msg_internal_allocation",
        execution_id="intr_internal_allocation",
        admission_idempotency_key="adm_internal_allocation",
    )

    dispatch = await service.start_job(
        "session_internal_allocation",
        "internal message",
        thread_id="thr_session_internal_allocation",
        execution_binding=execution_binding,
        message_id=execution_binding.message_id,
        message_created_at="2026-07-17T00:00:00+00:00",
    )

    assert dispatch.job_id == "job_allocated_once"
    assert reservations == ["allocated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved_job_id", ["", " \t\n"])
async def test_blank_reserved_internal_job_id_fails_before_preparation(
    reserved_job_id: str,
    monkeypatch: pytest.MonkeyPatch,
    session_lifecycle_guard,
) -> None:
    service = _service(monkeypatch, session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_blank_reserved_internal_job_id"
    reservations: list[str] = []
    starts: list[str] = []
    preparations: list[str] = []
    monkeypatch.setattr(
        service,
        "reserve_internal_job_id",
        lambda: reservations.append("allocated") or "job_unexpected",
    )
    monkeypatch.setattr(service, "_start_job_task", lambda job: starts.append(job.job_id))
    original_start_job_prepared = service._start_job_prepared

    async def tracked_start_job_prepared(*args, **kwargs):
        preparations.append("called")
        return await original_start_job_prepared(*args, **kwargs)

    monkeypatch.setattr(service, "_start_job_prepared", tracked_start_job_prepared)
    execution_binding = InternalExecutionIntent(
        message_id="msg_blank_reserved_internal_job_id",
        execution_id="intr_blank_reserved_internal_job_id",
        admission_idempotency_key="adm_blank_reserved_internal_job_id",
    )

    with pytest.raises(ValueError, match="预留 Job ID 必须是非空字符串"):
        await service.start_job(
            session_id,
            "internal message",
            thread_id=f"thr_{session_id}",
            execution_binding=execution_binding,
            reserved_job_id=reserved_job_id,
            message_id=execution_binding.message_id,
            message_created_at="2026-07-17T00:00:00+00:00",
        )

    assert reservations == []
    assert preparations == []
    assert starts == []
    assert service._jobs == {}
    assert service._pending_queue.ids(session_id) == ()


@pytest.mark.asyncio
async def test_pending_request_projects_nullable_turn_id_from_execution_binding(
    monkeypatch, session_lifecycle_guard
):
    service = _service(monkeypatch, session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_pending_turn_projection"
    await service.start_job(
        session_id,
        "active",
        thread_id=f"thr_{session_id}",
        message_id="msg_active_turn_projection",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    user_job = await service.start_job(
        session_id,
        "queued user message",
        thread_id=f"thr_{session_id}",
        message_id="msg_user_turn_projection",
        message_created_at="2026-07-17T00:00:01+00:00",
    )
    internal_binding = InternalExecutionIntent(
        message_id="msg_internal_turn_projection",
        execution_id="intr_turn_projection",
        admission_idempotency_key="adm_turn_projection",
    )
    internal_job = await service.start_job(
        session_id,
        "queued internal message",
        thread_id=f"thr_{session_id}",
        message_id=internal_binding.message_id,
        message_created_at="2026-07-17T00:00:02+00:00",
        execution_binding=internal_binding,
    )

    snapshot = await service.list_pending(session_id)
    user_request = next(
        item for item in snapshot.requests if item.job_id == user_job.job_id
    )
    internal_request = next(
        item for item in snapshot.requests if item.job_id == internal_job.job_id
    )
    user_binding = service.get_execution_binding(user_job.job_id)

    assert isinstance(user_binding, UserExecutionIdentity)
    assert user_request.turn_id == user_binding.turn_id
    assert user_request.turn_id != user_job.job_id
    assert internal_request.turn_id is None


def test_execution_bindings_reject_reused_identity_values():
    with pytest.raises(ValueError, match="彼此独立"):
        UserExecutionIdentity(
            job_id="job_same",
            turn_id="job_same",
            admission_idempotency_key="adm_other",
        )

    with pytest.raises(ValueError, match="彼此独立"):
        InternalExecutionIntent(
            message_id="msg_same",
            execution_id="intr_same",
            admission_idempotency_key="intr_same",
        )


@pytest.mark.asyncio
async def test_pending_request_uses_safe_display_content(
    monkeypatch, session_lifecycle_guard
):
    service = _service(monkeypatch, session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_internal_display"
    await service.start_job(
        session_id,
        "active",
        thread_id=f"thr_{session_id}",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    prepared = internal_message_factory.build(
        kind="generated_session_result",
        control="处理生成结果。",
        sections=(
            PromptSection("control_context", {"secret_route": "ses_private"}),
            PromptSection("generated_session_result", "内部结果"),
        ),
        display_content="生成分支已结束，主会话正在处理返回结果。",
    )
    await service.start_job(
        session_id,
        prepared.content,
        thread_id=f"thr_{session_id}",
        message_id="msg_internal",
        message_created_at="2026-07-17T00:00:01+00:00",
        message_metadata=prepared.metadata,
    )

    snapshot = await service.list_pending(session_id)

    internal = next(
        item for item in snapshot.requests if item.message_id == "msg_internal"
    )
    assert internal.content == "生成分支已结束，主会话正在处理返回结果。"
    assert "generated_session_result" not in internal.content
    assert "secret_route" not in internal.message_metadata


@pytest.mark.asyncio
async def test_stale_policy_update_is_rejected(monkeypatch, session_lifecycle_guard):
    service = _service(monkeypatch, session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_stale_policy"
    await service.start_job(
        session_id,
        "active",
        thread_id=f"thr_{session_id}",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "queued",
        thread_id=f"thr_{session_id}",
        message_id="msg_queued",
        message_created_at="2026-07-17T00:00:01+00:00",
    )

    with pytest.raises(RuntimeError, match="快照已过期"):
        await service.update_pending_policy(
            session_id,
            "msg_queued",
            delivery_policy="after_interrupt",
            expected_snapshot_version=(queued.queue_snapshot_version or 0) - 1,
        )


@pytest.mark.asyncio
async def test_promoted_pending_head_exposes_no_waiting_reason(
    monkeypatch, session_lifecycle_guard
):
    """待处理 DTO 不得把「等待队首」暴露给已升为队首的条目。

    改前 ``_bump`` 用「已有理由 or 新理由」计算位置理由，写过的「等待队首」
    永不失效：撤回原队首后，接替者在其 DTO.waiting_reason 里仍对外宣称
    「等待队首」，前端 PendingQueueBar 会把它渲染成 hover 提示，而此时它恰恰
    就是可立即投递的队首。
    """
    service = _service(monkeypatch, session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_promoted_head"
    await service.start_job(
        session_id,
        "active",
        thread_id=f"thr_{session_id}",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    await service.start_job(
        session_id,
        "queued",
        thread_id=f"thr_{session_id}",
        message_id="msg_queued",
        message_created_at="2026-07-17T00:00:01+00:00",
        delivery_policy="after_turn",
    )
    await service.start_job(
        session_id,
        "tail",
        thread_id=f"thr_{session_id}",
        message_id="msg_tail",
        message_created_at="2026-07-17T00:00:02+00:00",
        delivery_policy="after_turn",
    )

    wait_reason_before = {
        item.message_id: item.waiting_reason
        for item in (await service.list_pending(session_id)).requests
    }
    assert wait_reason_before == {"msg_queued": None, "msg_tail": "等待队首"}

    after_remove = await service.remove_pending(session_id, "msg_queued")

    promoted = next(
        item for item in after_remove.requests if item.message_id == "msg_tail"
    )
    assert promoted.waiting_reason is None
