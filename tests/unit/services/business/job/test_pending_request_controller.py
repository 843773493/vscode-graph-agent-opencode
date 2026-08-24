import pytest

from app.core.job_event_bus import JobEventBus
from app.prompting import PromptSection, internal_message_factory
from app.services.business.job.service import JobService


class _DummyJobExecutor:
    async def run(self, job):
        return "ok"


class _DummyTask:
    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        return None


def _service(monkeypatch: pytest.MonkeyPatch) -> JobService:
    service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=_DummyJobExecutor(),
    )
    monkeypatch.setattr(
        service,
        "_start_job_task",
        lambda job: setattr(job, "task", _DummyTask()),
    )
    return service


@pytest.mark.asyncio
async def test_pending_controls_edit_policy_remove_without_reordering(monkeypatch):
    service = _service(monkeypatch)
    session_id = "session_pending_controls"
    await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "queued",
        message_id="msg_queued",
        message_created_at="2026-07-17T00:00:01+00:00",
        delivery_policy="after_turn",
    )
    tail = await service.start_job(
        session_id,
        "tail",
        message_id="msg_tail",
        message_created_at="2026-07-17T00:00:02+00:00",
        delivery_policy="after_interrupt",
    )

    snapshot = await service.list_pending(session_id)
    assert queued.enqueue_sequence is not None
    assert tail.enqueue_sequence is not None
    assert [item.message_id for item in snapshot.requests] == [
        "msg_queued",
        "msg_tail",
    ]
    assert [item.enqueue_sequence for item in snapshot.requests] == [2, 3]
    assert all(item.status == "queued" for item in snapshot.requests)

    updated = await service.update_pending(
        session_id,
        "msg_tail",
        content="tail edited",
        attachments=[],
    )
    assert next(item for item in updated.requests if item.message_id == "msg_tail").content == (
        "tail edited"
    )

    changed = await service.update_pending_policy(
        session_id,
        "msg_tail",
        delivery_policy="after_tool_result",
        expected_snapshot_version=updated.snapshot_version,
    )
    changed_tail = next(item for item in changed.requests if item.message_id == "msg_tail")
    assert changed_tail.delivery_policy == "after_tool_result"
    assert [item.message_id for item in changed.requests] == [
        "msg_queued",
        "msg_tail",
    ]

    with pytest.raises(ValueError, match="不支持重排"):
        await service.reject_pending_reorder(session_id)

    after_remove = await service.remove_pending(session_id, "msg_queued")
    assert [item.message_id for item in after_remove.requests] == [
        "msg_tail",
    ]


@pytest.mark.asyncio
async def test_pending_request_uses_safe_display_content(monkeypatch):
    service = _service(monkeypatch)
    session_id = "session_internal_display"
    await service.start_job(
        session_id,
        "active",
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
        metadata={"secret_route": "ses_private"},
        display_content="生成分支已结束，主会话正在处理返回结果。",
    )
    await service.start_job(
        session_id,
        prepared.content,
        message_id="msg_internal",
        message_created_at="2026-07-17T00:00:01+00:00",
        message_metadata=prepared.metadata,
    )

    snapshot = await service.list_pending(session_id)

    internal = next(item for item in snapshot.requests if item.message_id == "msg_internal")
    assert internal.content == "生成分支已结束，主会话正在处理返回结果。"
    assert "generated_session_result" not in internal.content
    assert "secret_route" not in internal.message_metadata


@pytest.mark.asyncio
async def test_stale_policy_update_is_rejected(monkeypatch):
    service = _service(monkeypatch)
    session_id = "session_stale_policy"
    await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-17T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "queued",
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
