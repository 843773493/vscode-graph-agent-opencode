from __future__ import annotations

import asyncio

import pytest

from app.core.job_event_bus import EventType
from app.prompting import internal_message_factory
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import JobService, JobState


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, **event: object) -> None:
        self.events.append(event)


class _NeverFinishExecutor:
    async def run(self, _job: object) -> str:
        await asyncio.Future()
        raise AssertionError("不可达")


class _CancellationResistantExecutor:
    async def run(self, _job: object) -> str:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # 模拟 provider/工具在第一次取消后仍卡在自己的清理等待中。
            await asyncio.Future()
        raise AssertionError("不可达")


class _BlockingExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.runtime_state: object | None = None

    async def run(self, state: object) -> str:
        self.runtime_state = state
        self.started.set()
        await asyncio.Future()
        raise AssertionError("不可达")


class _AgentStartThenNeverFinishExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, state: object) -> str:
        reporter = getattr(state, "progress_reporter", None)
        if not callable(reporter):
            raise TypeError("Job runtime state 未提供 progress_reporter")
        reporter("agent_start")
        self.started.set()
        await asyncio.Future()
        raise AssertionError("不可达")


class _AgentLoopReadyThenNeverFinishExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_reason: str | None = None

    async def run(self, state: object) -> str:
        reporter = getattr(state, "progress_reporter", None)
        if not callable(reporter):
            raise TypeError("Job runtime state 未提供 progress_reporter")
        reporter("agent_start")
        reporter("agent_loop_ready")
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as error:
            self.cancel_reason = str(error)
            raise
        raise AssertionError("不可达")


class _ShortMessageExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, state: object) -> str:
        reporter = getattr(state, "progress_reporter", None)
        if not callable(reporter):
            raise TypeError("Job runtime state 未提供 progress_reporter")
        self.calls += 1
        reporter("agent_start")
        reporter("agent_loop_ready")
        reporter("model")
        return f"short-message-ok-{self.calls}"


class _CodedFailure(RuntimeError):
    code = "agent_event_timeout"


class _CodedFailureExecutor:
    async def run(self, _state: object) -> str:
        raise _CodedFailure("等待首个模型/工具事件超时")


class _ProgressThenNeverFinishExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_reason: str | None = None

    async def run(self, state: object) -> str:
        reporter = getattr(state, "progress_reporter", None)
        if not callable(reporter):
            raise TypeError("Job runtime state 未提供 progress_reporter")
        reporter("tool:apply_patch")
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as error:
            self.cancel_reason = str(error)
            raise
        raise AssertionError("不可达")


class _ModelFinalizationExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, state: object) -> str:
        reporter = getattr(state, "progress_reporter", None)
        if not callable(reporter):
            raise TypeError("Job runtime state 未提供 progress_reporter")
        reporter("agent_start")
        reporter("agent_loop_ready")
        reporter("model")
        self.started.set()
        await self.release.wait()
        return "final-response-after-grace"


class _ToolFinalizationExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, state: object) -> str:
        reporter = getattr(state, "progress_reporter", None)
        if not callable(reporter):
            raise TypeError("Job runtime state 未提供 progress_reporter")
        reporter("agent_start")
        reporter("agent_loop_ready")
        reporter("tool:runPlaywrightCode")
        self.started.set()
        await self.release.wait()
        return "browser-result-and-final-response"


class _StaleStepFinalizationExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, state: object) -> str:
        reporter = getattr(state, "progress_reporter", None)
        if not callable(reporter):
            raise TypeError("Job runtime state 未提供 progress_reporter")
        reporter("agent_start")
        reporter("agent_loop_ready")
        self.started.set()
        await self.release.wait()
        return "final-response-after-stale-step"


class _RecordingTurnStatusWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
    ) -> bool:
        self.calls.append({
            "session_id": session_id,
            "turn_id": turn_id,
            "status": status,
        })
        return True


@pytest.mark.asyncio
async def test_job_timeout_marks_job_terminal_and_releases_session() -> None:
    bus = _RecordingBus()
    writer = _RecordingTurnStatusWriter()
    service = JobService(
        job_event_bus=bus,
        job_executor=_NeverFinishExecutor(),
        job_timeout_seconds=0.01,
        terminal_status_writer=writer,
    )
    session_id = "session_job_timeout"
    job = JobState(
        job_id="job_timeout",
        session_id=session_id,
        message="不会完成",
        message_id="msg_timeout",
        message_created_at="2026-08-31T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    assert job.task is not None
    await job.task

    assert job.status == JobStatus.timed_out
    assert job.error_message is not None
    assert "总超时上限" in job.error_message
    assert service._session_current_job.get(session_id) is None
    failed_events = [
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["payload"] == {
        "session_id": session_id,
        "error": job.error_message,
        "code": "job_timeout",
        "timeout_seconds": 0.01,
    }
    assert writer.calls == [{
        "session_id": session_id,
        "turn_id": job.job_id,
        "status": "timed_out",
    }]


@pytest.mark.asyncio
async def test_job_timeout_does_not_wait_for_uncooperative_executor() -> None:
    service = JobService(
        job_event_bus=_RecordingBus(),
        job_executor=_CancellationResistantExecutor(),
        job_timeout_seconds=0.01,
        execution_cancel_timeout_seconds=0.01,
    )
    job = JobState(
        job_id="job_uncooperative_timeout",
        session_id="session_uncooperative_timeout",
        message="超时后执行器不配合取消",
        message_id="msg_uncooperative_timeout",
        message_created_at="2026-08-31T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(job.session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(job.session_id) is True
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=0.2)

    assert job.status == JobStatus.timed_out
    assert service._session_current_job.get(job.session_id) is None


@pytest.mark.asyncio
async def test_job_startup_timeout_closes_job_without_agent_start() -> None:
    bus = _RecordingBus()
    writer = _RecordingTurnStatusWriter()
    service = JobService(
        job_event_bus=bus,
        job_executor=_NeverFinishExecutor(),
        job_timeout_seconds=1.0,
        job_startup_timeout_seconds=0.01,
        terminal_status_writer=writer,
    )
    session_id = "session_job_startup_timeout"
    job = JobState(
        job_id="job_startup_timeout",
        session_id=session_id,
        message="启动后不产生 agent_start",
        message_id="msg_startup_timeout",
        message_created_at="2026-08-31T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    assert job.task is not None
    await job.task

    assert job.status == JobStatus.timed_out
    assert job.error_message is not None
    assert "启动超过等待 AgentLoop 的上限" in job.error_message
    failed_event = next(
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    )
    assert failed_event["payload"]["code"] == "job_startup_timeout"
    assert writer.calls == [{
        "session_id": session_id,
        "turn_id": job.job_id,
        "status": "timed_out",
    }]


@pytest.mark.asyncio
async def test_agent_start_does_not_mask_first_model_or_tool_startup_timeout() -> None:
    bus = _RecordingBus()
    executor = _AgentStartThenNeverFinishExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=1.0,
        job_startup_timeout_seconds=0.01,
    )
    session_id = "session_agent_start_without_progress"
    job = JobState(
        job_id="job_agent_start_without_progress",
        session_id=session_id,
        message="只报告 agent_start 后不再推进",
        message_id="msg_agent_start_without_progress",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    await asyncio.wait_for(executor.started.wait(), timeout=0.1)
    assert job.task is not None
    await job.task

    assert job.status == JobStatus.timed_out
    failed_event = next(
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    )
    assert failed_event["payload"]["code"] == "job_startup_timeout"


@pytest.mark.asyncio
async def test_job_failure_event_preserves_executor_error_code() -> None:
    bus = _RecordingBus()
    service = JobService(
        job_event_bus=bus,
        job_executor=_CodedFailureExecutor(),
    )
    session_id = "session_coded_job_failure"
    job = JobState(
        job_id="job_coded_failure",
        session_id=session_id,
        message="首事件超时",
        message_id="msg_coded_failure",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    assert job.task is not None
    await job.task

    failed_event = next(
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    )
    assert failed_event["payload"] == {
        "session_id": session_id,
        "error": "等待首个模型/工具事件超时",
        "code": "agent_event_timeout",
    }


@pytest.mark.asyncio
async def test_agent_loop_ready_uses_event_watchdog_not_startup_watchdog() -> None:
    bus = _RecordingBus()
    executor = _AgentLoopReadyThenNeverFinishExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=0.02,
        job_startup_timeout_seconds=0.01,
    )
    session_id = "session_agent_loop_ready"
    job = JobState(
        job_id="job_agent_loop_ready",
        session_id=session_id,
        message="runtime 已就绪但模型不推进",
        message_id="msg_agent_loop_ready",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    await asyncio.wait_for(executor.started.wait(), timeout=0.1)
    assert job.task is not None
    await job.task

    assert executor.cancel_reason == "job_timeout"
    assert job.status == JobStatus.timed_out
    timeout_event = next(
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    )
    assert timeout_event["payload"]["code"] == "job_timeout"


@pytest.mark.asyncio
async def test_two_short_messages_start_and_converge_after_previous_turn() -> None:
    bus = _RecordingBus()
    executor = _ShortMessageExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=0.2,
        job_startup_timeout_seconds=0.02,
    )
    session_id = "session_two_short_messages"

    jobs: list[JobState] = []
    for index in range(2):
        job = JobState(
            job_id=f"job_short_message_{index}",
            session_id=session_id,
            message=f"短消息 {index}",
            message_id=f"msg_short_message_{index}",
            message_created_at="2026-09-01T00:00:00+00:00",
            agent_id="default",
            status=JobStatus.queued,
        )
        jobs.append(job)
        service._jobs[job.job_id] = job
        service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    for job in jobs:
        assert job.task is not None
        await job.task

    assert executor.calls == 2
    assert [job.status for job in jobs] == [JobStatus.completed, JobStatus.completed]
    assert [job.result for job in jobs] == [
        "short-message-ok-1",
        "short-message-ok-2",
    ]
    assert service._session_current_job.get(session_id) is None


@pytest.mark.asyncio
async def test_running_job_exposes_progress_and_current_step_before_executor_event() -> None:
    bus = _RecordingBus()
    executor = _BlockingExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=2.0,
    )
    session_id = "session_job_progress"
    job = JobState(
        job_id="job_progress",
        session_id=session_id,
        message="等待首个模型事件",
        message_id="msg_progress",
        message_created_at="2026-08-31T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")
    queued_updated_at = job.updated_at

    assert await service._start_next_pending(session_id) is True
    await asyncio.wait_for(executor.started.wait(), timeout=0.1)

    observed = await service.get(job.job_id)
    assert observed.status == JobStatus.running
    assert observed.progress == 1
    assert observed.current_step == "agent_execution"
    assert observed.updated_at > queued_updated_at

    await asyncio.sleep(1.05)
    refreshed = await service.get(job.job_id)
    assert refreshed.updated_at > observed.updated_at

    assert job.task is not None
    await job.task
    assert job.status == JobStatus.timed_out


@pytest.mark.asyncio
async def test_tool_progress_refreshes_job_and_timeout_has_distinct_cancel_reason() -> None:
    bus = _RecordingBus()
    executor = _ProgressThenNeverFinishExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=0.02,
    )
    session_id = "session_job_tool_progress"
    job = JobState(
        job_id="job_tool_progress",
        session_id=session_id,
        message="执行工具后等待",
        message_id="msg_tool_progress",
        message_created_at="2026-08-31T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    await asyncio.wait_for(executor.started.wait(), timeout=0.1)
    assert job.progress >= 2
    assert job.current_step == "tool:apply_patch"

    assert job.task is not None
    await job.task

    assert executor.cancel_reason == "job_timeout"
    assert job.status == JobStatus.timed_out
    assert job.current_step is None
    assert job.progress < 100
    timeout_event = next(
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    )
    assert timeout_event["payload"]["code"] == "job_timeout"


@pytest.mark.asyncio
async def test_model_finalization_grace_preserves_response_after_total_budget() -> None:
    bus = _RecordingBus()
    executor = _ModelFinalizationExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=0.02,
        job_finalization_grace_seconds=0.1,
    )
    session_id = "session_model_finalization_grace"
    job = JobState(
        job_id="job_model_finalization_grace",
        session_id=session_id,
        message="工具已完成，等待最终回复",
        message_id="msg_model_finalization_grace",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    await asyncio.wait_for(executor.started.wait(), timeout=0.1)
    await asyncio.sleep(0.03)
    executor.release.set()

    assert job.task is not None
    await job.task

    assert job.status == JobStatus.completed
    assert job.result == "final-response-after-grace"
    assert not [
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    ]


@pytest.mark.asyncio
async def test_tool_finalization_grace_preserves_result_after_total_budget() -> None:
    bus = _RecordingBus()
    executor = _ToolFinalizationExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=0.02,
        job_finalization_grace_seconds=0.1,
    )
    session_id = "session_tool_finalization_grace"
    job = JobState(
        job_id="job_tool_finalization_grace",
        session_id=session_id,
        message="浏览器工具已开始，等待结果",
        message_id="msg_tool_finalization_grace",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    await asyncio.wait_for(executor.started.wait(), timeout=0.1)
    await asyncio.sleep(0.03)
    executor.release.set()

    assert job.task is not None
    await job.task

    assert job.status == JobStatus.completed
    assert job.result == "browser-result-and-final-response"
    assert job.current_step is None
    assert not [
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    ]


@pytest.mark.asyncio
async def test_finalization_grace_does_not_require_fresh_step_projection() -> None:
    bus = _RecordingBus()
    executor = _StaleStepFinalizationExecutor()
    service = JobService(
        job_event_bus=bus,
        job_executor=executor,
        job_timeout_seconds=0.02,
        job_finalization_grace_seconds=0.1,
    )
    session_id = "session_stale_step_finalization"
    job = JobState(
        job_id="job_stale_step_finalization",
        session_id=session_id,
        message="步骤投影滞后但执行仍在收尾",
        message_id="msg_stale_step_finalization",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")

    assert await service._start_next_pending(session_id) is True
    await asyncio.wait_for(executor.started.wait(), timeout=0.1)
    await asyncio.sleep(0.03)
    executor.release.set()

    assert job.task is not None
    await job.task

    assert job.status == JobStatus.completed
    assert job.result == "final-response-after-stale-step"
    assert not [
        event
        for event in bus.events
        if event.get("event_type") == EventType.JOB_FAILED
    ]


@pytest.mark.asyncio
async def test_failed_job_discards_queued_terminal_followup_without_new_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = _RecordingBus()
    service = JobService(
        job_event_bus=bus,
        job_executor=_NeverFinishExecutor(),
    )
    session_id = "session_failed_terminal_followup"
    parent = JobState(
        job_id="job_parent_failed",
        session_id=session_id,
        message="browser readPage failed",
        message_id="msg_parent_failed",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.failed,
        delivery_policy="after_turn",
        delivery_boundary="idle",
    )
    internal_message = internal_message_factory.build(
        kind="terminal_execution_completed",
        control="读取终端最终输出。",
        metadata={"terminal_id": "term_completed"},
    )
    followup = JobState(
        job_id="job_terminal_followup",
        session_id=session_id,
        message=internal_message.content,
        message_id="msg_terminal_followup",
        message_created_at="2026-09-01T00:00:01+00:00",
        agent_id="default",
        status=JobStatus.queued,
        message_metadata=internal_message.metadata,
        delivery_policy="after_tool_result",
    )
    user_job = JobState(
        job_id="job_user_retry",
        session_id=session_id,
        message="retry",
        message_id="msg_user_retry",
        message_created_at="2026-09-01T00:00:02+00:00",
        agent_id="default",
        status=JobStatus.queued,
        delivery_policy="after_turn",
    )
    service._jobs.update({
        parent.job_id: parent,
        followup.job_id: followup,
        user_job.job_id: user_job,
    })
    service._session_current_job[session_id] = parent.job_id
    service._pending_queue.append(
        session_id,
        followup.job_id,
        "after_tool_result",
    )
    service._pending_queue.append(session_id, user_job.job_id, "after_turn")
    started: list[str] = []

    def start_job_task(job: JobState) -> None:
        started.append(job.job_id)
        job.task = None

    monkeypatch.setattr(service, "_start_job_task", start_job_task)

    await service._schedule_next_job_if_needed(parent)

    assert followup.status == JobStatus.failed
    assert service._pending_queue.ids(session_id) == ()
    assert started == [user_job.job_id]
    assert service._session_current_job[session_id] == user_job.job_id
    stale_event = next(
        event
        for event in bus.events
        if event.get("job_id") == followup.job_id
        and event.get("event_type") == EventType.JOB_FAILED
    )
    assert stale_event["payload"]["code"] == "stale_internal_followup"


@pytest.mark.asyncio
async def test_pending_dispatch_race_cannot_start_terminal_followup_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = JobService(
        job_event_bus=_RecordingBus(),
        job_executor=_NeverFinishExecutor(),
    )
    session_id = "session_failed_followup_race"
    parent = JobState(
        job_id="job_failed_race_parent",
        session_id=session_id,
        message="failed",
        message_id="msg_failed_race_parent",
        message_created_at="2026-09-01T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.failed,
    )
    internal_message = internal_message_factory.build(
        kind="terminal_execution_completed",
        control="读取终端最终输出。",
        metadata={"terminal_id": "term_completed"},
    )
    followup = JobState(
        job_id="job_failed_race_followup",
        session_id=session_id,
        message=internal_message.content,
        message_id="msg_failed_race_followup",
        message_created_at="2026-09-01T00:00:01+00:00",
        agent_id="default",
        status=JobStatus.queued,
        message_metadata=internal_message.metadata,
        delivery_policy="after_tool_result",
    )
    service._jobs.update({parent.job_id: parent, followup.job_id: followup})
    service._session_current_job[session_id] = parent.job_id
    service._pending_queue.append(session_id, followup.job_id, "after_tool_result")
    started: list[str] = []
    monkeypatch.setattr(
        service,
        "_start_job_task",
        lambda job: started.append(job.job_id),
    )

    assert await service._start_next_pending(session_id, boundary="idle") is False
    assert followup.status == JobStatus.failed
    assert started == []
    assert service._pending_queue.ids(session_id) == ()
