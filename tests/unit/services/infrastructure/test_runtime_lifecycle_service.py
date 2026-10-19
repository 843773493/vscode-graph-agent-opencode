from __future__ import annotations

import asyncio
import json
import weakref
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.schemas.event import (
    JobCreatedEvent,
    JobCreatedPayload,
    JobFailedEvent,
    JobFailedPayload,
    JobStartedEvent,
    JobStartedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
)
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import (
    JobAdmissionClosedError,
    JobService,
    JobState,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.support.background_work import build_background_registry


class RecordingJobEventBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, **event: object) -> None:
        self.events.append(event)


class NeverFinishExecutor:
    async def run(self, _state: object) -> str:
        await asyncio.Future()
        raise AssertionError("不可达")


class RecordingTurnStatusWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def mark_job_terminal_status(
        self,
        *,
        session_id: str,
        thread_id: str,
        job_id: str,
        status: str,
    ) -> bool:
        self.calls.append(
            {"session_id": session_id, "thread_id": thread_id, "job_id": job_id, "status": status}
        )
        return True


@pytest.fixture
def pending_checkpoint_inspector():
    async def inspect(_job: JobState) -> None:
        return None
    return inspect


@pytest.fixture
def before_provider_inspector():
    async def inspect(_job: JobState) -> bool:
        return False
    return inspect


def build_runtime(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    terminal_status_writer: RecordingTurnStatusWriter | None = None,
    *,
    session_lifecycle_guard,
) -> tuple[RuntimeService, JobService]:
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    bus = RecordingJobEventBus()
    jobs = JobService(
        job_event_bus=bus,
        job_executor=NeverFinishExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )
    runtime = RuntimeService(
        workspace_id="00000000-0000-4000-8000-000000000001",
        workspace_root=tmp_path,
        job_service=jobs,
        pending_tool_checkpoint_id=pending_checkpoint_inspector,
        can_restart_before_provider=before_provider_inspector,
        background_task_registry=build_background_registry(sessions_dir),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        message_stream_store=MessageStreamStore(
            # R18 catalog 模式适配：经 path_utils 开关工厂取 resolver
            # （catalog 模式走 SQLite catalog 链，旧模式返回原 legacy
            # resolver，行为不变）。直连旧类会在 catalog 模式下解析不到
            # 经 factory 注册的会话。
            path_resolver=get_session_path_resolver(sessions_dir)
        ),
        terminal_status_writer=terminal_status_writer,
    )
    return runtime, jobs


@pytest.mark.asyncio
async def test_status_reports_the_injected_workspace_uuid_without_storage_location(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path, session_lifecycle_guard=session_lifecycle_guard
    )

    status = await runtime.status()

    assert status.workspace_id == "00000000-0000-4000-8000-000000000001"
    assert "storage" not in status.model_dump()
    assert str(tmp_path) not in status.model_dump_json()
    assert runtime.get_log_dir() == tmp_path / ".boxteam" / "logs"


@pytest.mark.asyncio
async def test_drain_closes_admission_and_cancel_reopens_it(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path, session_lifecycle_guard=session_lifecycle_guard
    )

    draining = await runtime.begin_drain()

    assert draining.lifecycle_state == "draining"
    assert not draining.accepting_jobs
    with pytest.raises(JobAdmissionClosedError):
        jobs.assert_accepting_jobs()

    ready = await runtime.cancel_drain()

    assert ready.lifecycle_state == "ready"
    assert ready.accepting_jobs
    jobs.assert_accepting_jobs()


@pytest.mark.asyncio
async def test_force_interrupt_persists_event_and_cancels_job(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path, session_lifecycle_guard=session_lifecycle_guard
    )
    task = asyncio.create_task(asyncio.sleep(60))
    job = JobState(
        job_id="job_running",
        session_id="ses_runtime",
        message="运行中",
        message_id="msg_runtime",
        message_created_at=datetime.now(UTC).isoformat(),
        agent_id="default",
        status=JobStatus.running,
        task=task,
        thread_id=create_prefixed_id("thr"),
        execution_binding=UserExecutionIdentity(job_id="job_running", turn_id=create_prefixed_id("turn"), admission_idempotency_key=create_prefixed_id("adm")),
    )
    jobs._jobs[job.job_id] = job
    jobs._thread_current_job[(job.session_id, job.thread_id)] = job.job_id

    with workspace_context(runtime._workspace_id, tmp_path):
        draining = await runtime.begin_drain()
        forced = await runtime.force_interrupt()

    assert [blocker.resource_id for blocker in draining.blockers] == ["job_running"]
    assert forced.lifecycle_state == "stopping"
    assert forced.interrupted_resources == 1
    assert task.cancelled()
    assert job.status == JobStatus.cancelled
    assert job.error_message == "Gateway 显式强制重启 Workspace API"


@pytest.mark.asyncio
async def test_startup_reconciles_job_without_terminal_event(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    session_bundle_factory(
        tmp_path / ".boxteam" / "sessions", "ses_019c70f868da751480de63aa344e28c9"
    )
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path, session_lifecycle_guard=session_lifecycle_guard
    )
    store = runtime._trace_event_store
    now = datetime.now(UTC)
    await store.append(
        "ses_019c70f868da751480de63aa344e28c9",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=now,
            payload=JobStartedPayload(session_id="ses_019c70f868da751480de63aa344e28c9", thread_id=get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c70f868da751480de63aa344e28c9")),
        ),
    )

    reconciled = await runtime.reconcile_stale_executions()
    events = store.read_events("ses_019c70f868da751480de63aa344e28c9")

    assert reconciled == 1
    assert events[-1].type == "session_interrupted"
    assert events[-1].payload.phase == "process_exit"


@pytest.mark.asyncio
async def test_startup_rejects_invalid_trace(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    session_root = session_bundle_factory(
        tmp_path / ".boxteam" / "sessions", "ses_019b7e56befb747788f3efc9aace9dbc"
    )
    trace_file = session_root / "logs" / "traces" / "events.jsonl"
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    trace_file.write_text(
        json.dumps(
            {
                "event_id": "evt_legacy_text_start",
                "job_id": "job_legacy",
                "step_id": None,
                "agent_id": "default",
                "timestamp": "2026-07-05T14:51:24.999582+00:00",
                "type": "text_start",
                "payload": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path, session_lifecycle_guard=session_lifecycle_guard
    )

    with pytest.raises(RuntimeError, match="Trace 事件协议无效"):
        await runtime.reconcile_stale_executions()


@pytest.mark.asyncio
async def test_startup_reconciles_persisted_turn_as_failed(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    session_bundle_factory(
        tmp_path / ".boxteam" / "sessions", "ses_019c70f868da751480de63aa344e28c9"
    )
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path,
        terminal_status_writer=writer,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    await runtime._trace_event_store.append(
        "ses_019c70f868da751480de63aa344e28c9",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=datetime.now(UTC),
            payload=JobStartedPayload(session_id="ses_019c70f868da751480de63aa344e28c9", thread_id=get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c70f868da751480de63aa344e28c9")),
        ),
    )

    await runtime.reconcile_stale_executions()

    assert writer.calls == [
        {
            "session_id": "ses_019c70f868da751480de63aa344e28c9",
            "thread_id": get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c70f868da751480de63aa344e28c9"),
            "job_id": "job_stale",
            "status": "execution_lost",
        }
    ]
    event = runtime._trace_event_store.read_events(
        "ses_019c70f868da751480de63aa344e28c9"
    )[-1]
    assert event.payload.code == "execution_lost"
    assert event.payload.resumable is False


@pytest.mark.asyncio
async def test_startup_repairs_existing_process_exit_turn_status(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    session_bundle_factory(
        tmp_path / ".boxteam" / "sessions", "ses_019c70f868da751480de63aa344e28c9"
    )
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path,
        terminal_status_writer=writer,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    now = datetime.now(UTC)
    await runtime._trace_event_store.append(
        "ses_019c70f868da751480de63aa344e28c9",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=now,
            payload=JobStartedPayload(session_id="ses_019c70f868da751480de63aa344e28c9", thread_id=get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c70f868da751480de63aa344e28c9")),
        ),
    )
    await runtime._trace_event_store.append(
        "ses_019c70f868da751480de63aa344e28c9",
        SessionInterruptedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=now,
            payload=SessionInterruptedPayload(
                session_id="ses_019c70f868da751480de63aa344e28c9",
                phase="process_exit",
                code="execution_lost",
                message="工作区后端重启，无法安全续接原 AgentLoop 执行",
            ),
        ),
    )

    reconciled = await runtime.reconcile_stale_executions()

    assert reconciled == 0
    assert writer.calls == [
        {
            "session_id": "ses_019c70f868da751480de63aa344e28c9",
            "thread_id": get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c70f868da751480de63aa344e28c9"),
            "job_id": "job_stale",
            "status": "execution_lost",
        }
    ]
    assert (
        len(
            runtime._trace_event_store.read_events(
                "ses_019c70f868da751480de63aa344e28c9"
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_startup_preserves_timeout_status_from_failed_event(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    session_bundle_factory(
        tmp_path / ".boxteam" / "sessions", "ses_019c162a2dfb7cf08123d546495a99c8"
    )
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path,
        terminal_status_writer=writer,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    await runtime._trace_event_store.append(
        "ses_019c162a2dfb7cf08123d546495a99c8",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"), job_id="job_timeout", timestamp=datetime.now(UTC),
            payload=JobStartedPayload(
                session_id="ses_019c162a2dfb7cf08123d546495a99c8",
                thread_id=get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c162a2dfb7cf08123d546495a99c8"),
            ),
        ),
    )
    await runtime._trace_event_store.append(
        "ses_019c162a2dfb7cf08123d546495a99c8",
        JobFailedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_timeout",
            timestamp=datetime.now(UTC),
            payload=JobFailedPayload(
                error="Job 执行超过总超时上限",
                code="job_timeout",
                timeout_seconds=600,
            ),
        ),
    )

    await runtime.reconcile_stale_executions()

    assert writer.calls == [
        {
            "session_id": "ses_019c162a2dfb7cf08123d546495a99c8",
            "thread_id": get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c162a2dfb7cf08123d546495a99c8"),
            "job_id": "job_timeout",
            "status": "timed_out",
        }
    ]


@pytest.mark.asyncio
async def test_startup_requires_structured_timeout_code(pending_checkpoint_inspector, before_provider_inspector,
    tmp_path: Path,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    session_bundle_factory(
        tmp_path / ".boxteam" / "sessions", "ses_019c1bbd6ff678cf8f3e8598a8ae7327"
    )
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        before_provider_inspector=before_provider_inspector,
        tmp_path=tmp_path,
        terminal_status_writer=writer,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    await runtime._trace_event_store.append(
        "ses_019c1bbd6ff678cf8f3e8598a8ae7327",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"), job_id="job_legacy_timeout", timestamp=datetime.now(UTC),
            payload=JobStartedPayload(
                session_id="ses_019c1bbd6ff678cf8f3e8598a8ae7327",
                thread_id=get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c1bbd6ff678cf8f3e8598a8ae7327"),
            ),
        ),
    )
    await runtime._trace_event_store.append(
        "ses_019c1bbd6ff678cf8f3e8598a8ae7327",
        JobFailedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_legacy_timeout",
            timestamp=datetime.now(UTC),
            payload=JobFailedPayload(error="Job 执行超过总超时上限"),
        ),
    )

    await runtime.reconcile_stale_executions()

    assert writer.calls == [
        {
            "session_id": "ses_019c1bbd6ff678cf8f3e8598a8ae7327",
            "thread_id": get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id("ses_019c1bbd6ff678cf8f3e8598a8ae7327"),
            "job_id": "job_legacy_timeout",
            "status": "failed",
        }
    ]


@pytest.fixture
def recovery_inspectors():
    calls: list[tuple[str, JobState]] = []

    async def before_provider(job: JobState) -> bool:
        calls.append(("before_provider", job))
        return False

    async def pending_checkpoint(job: JobState) -> str:
        calls.append(("pending_checkpoint", job))
        return "checkpoint-original"

    return calls, before_provider, pending_checkpoint


def _created_request(session_id: str, thread_id: str, job_id: str) -> JobCreatedEvent:
    return JobCreatedEvent(
        event_id=create_prefixed_id("evt"), job_id=job_id, timestamp=datetime.now(UTC),
        payload=JobCreatedPayload(
            session_id=session_id, thread_id=thread_id, message=f"原始消息 {thread_id}",
            agent_id="default", message_id=create_prefixed_id("msg"),
            message_created_at=datetime.now(UTC),
            execution_binding=UserExecutionIdentity(
                job_id=job_id, turn_id=create_prefixed_id("turn"),
                admission_idempotency_key=create_prefixed_id("adm"),
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_role", ["main", "child"])
async def test_startup_restores_only_the_matching_created_and_started_owner(
    tmp_path: Path, session_bundle_factory, session_lifecycle_guard,
    recovery_inspectors, thread_role: str,
) -> None:
    session_id = "ses_019c70f868da751480de63aa344e28c9"
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_dir, session_id)
    main_thread = get_session_path_resolver(sessions_dir).main_thread_id(session_id)
    thread_id = main_thread if thread_role == "main" else create_prefixed_id("thr")
    calls, before_provider, pending_checkpoint = recovery_inspectors
    writer = RecordingTurnStatusWriter()
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint,
        before_provider_inspector=before_provider, tmp_path=tmp_path,
        terminal_status_writer=writer, session_lifecycle_guard=session_lifecycle_guard,
    )
    request = _created_request(session_id, thread_id, "job-owner-match")
    # 同 owner 的重复 created/started 保持原恢复身份，不造成第二个 Job。
    for _ in range(2):
        await runtime._trace_event_store.append(session_id, request)
        await runtime._trace_event_store.append(session_id, JobStartedEvent(
            event_id=create_prefixed_id("evt"), job_id=request.job_id,
            timestamp=datetime.now(UTC),
            payload=JobStartedPayload(session_id=session_id, thread_id=thread_id),
        ))

    assert await runtime.reconcile_stale_executions() == 0

    assert [kind for kind, _ in calls] == ["before_provider", "pending_checkpoint"]
    restored = jobs._jobs[request.job_id]
    assert all(job is restored for _, job in calls)
    assert (restored.session_id, restored.thread_id) == (session_id, thread_id)
    assert restored.message == request.payload.message
    assert restored.message_id == request.payload.message_id
    assert restored.execution_binding == request.payload.execution_binding
    assert restored.resume_checkpoint_id == "checkpoint-original"
    assert jobs._thread_current_job == {(session_id, thread_id): request.job_id}
    assert restored.task is None
    assert writer.calls == []
    assert len(runtime._trace_event_store.read_events(session_id)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    "created_thread_vs_started", "created_session_vs_trace", "duplicate_created_owner",
    "cross_trace_created_owner", "cross_trace_started_owner", "cross_trace_terminal",
])
async def test_startup_rejects_owner_conflicts_before_any_recovery_side_effect(
    tmp_path: Path, session_bundle_factory, session_lifecycle_guard,
    recovery_inspectors, fault: str,
) -> None:
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_id = "ses_019c70f868da751480de63aa344e28c9"
    other_session = "ses_019d70f868da751480de63aa344e28c9"
    for owner_session in (session_id, other_session):
        session_bundle_factory(sessions_dir, owner_session)
    resolver = get_session_path_resolver(sessions_dir)
    main_thread = resolver.main_thread_id(session_id)
    other_thread = resolver.main_thread_id(other_session)
    calls, before_provider, pending_checkpoint = recovery_inspectors
    writer = RecordingTurnStatusWriter()
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint,
        before_provider_inspector=before_provider, tmp_path=tmp_path,
        terminal_status_writer=writer, session_lifecycle_guard=session_lifecycle_guard,
    )
    store = runtime._trace_event_store
    request = _created_request(session_id, main_thread, "job-conflicted")
    await store.append(session_id, request)
    started_thread = create_prefixed_id("thr") if fault == "created_thread_vs_started" else main_thread
    await store.append(session_id, JobStartedEvent(
        event_id=create_prefixed_id("evt"), job_id=request.job_id,
        timestamp=datetime.now(UTC),
        payload=JobStartedPayload(session_id=session_id, thread_id=started_thread),
    ))
    # 前面的合法 started 也不能在后面的冲突被发现前先写入 execution_lost。
    await store.append(session_id, JobStartedEvent(
        event_id=create_prefixed_id("evt"), job_id="job-valid-before-conflict",
        timestamp=datetime.now(UTC),
        payload=JobStartedPayload(session_id=session_id, thread_id=main_thread),
    ))
    if fault == "created_session_vs_trace":
        await store.append(session_id, _created_request(other_session, other_thread, "job-request-wrong-trace"))
    elif fault == "duplicate_created_owner":
        await store.append(session_id, _created_request(session_id, create_prefixed_id("thr"), request.job_id))
    elif fault == "cross_trace_created_owner":
        await store.append(other_session, _created_request(other_session, other_thread, request.job_id))
    elif fault == "cross_trace_started_owner":
        await store.append(other_session, JobStartedEvent(
            event_id=create_prefixed_id("evt"), job_id=request.job_id,
            timestamp=datetime.now(UTC),
            payload=JobStartedPayload(session_id=other_session, thread_id=other_thread),
        ))
    elif fault == "cross_trace_terminal":
        await store.append(other_session, JobFailedEvent(
            event_id=create_prefixed_id("evt"), job_id=request.job_id,
            timestamp=datetime.now(UTC), payload=JobFailedPayload(error="错误会话终态"),
        ))
    events_before = {owner: store.read_events(owner) for owner in (session_id, other_session)}

    with pytest.raises(RuntimeError, match="owner"):
        await runtime.reconcile_stale_executions()

    assert calls == []
    assert writer.calls == []
    assert jobs._jobs == {}
    assert jobs._thread_current_job == {}
    assert {owner: store.read_events(owner) for owner in (session_id, other_session)} == events_before


@pytest.mark.asyncio
async def test_startup_releases_other_session_request_bodies_before_recovery(
    tmp_path: Path, session_bundle_factory, session_lifecycle_guard, monkeypatch: pytest.MonkeyPatch,
    recovery_inspectors,
) -> None:
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    session_ids = (
        "ses_019c70f868da751480de63aa344e28c9",
        "ses_019d70f868da751480de63aa344e28c9",
    )
    calls, before_provider, pending_checkpoint = recovery_inspectors
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint,
        before_provider_inspector=before_provider, tmp_path=tmp_path,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    store = runtime._trace_event_store
    for index, session_id in enumerate(session_ids):
        session_bundle_factory(sessions_dir, session_id)
        thread_id = get_session_path_resolver(sessions_dir).main_thread_id(session_id)
        request = _created_request(session_id, thread_id, f"job-large-request-{index}")
        request.payload.message = "独立会话正文" * 200_000
        await store.append(session_id, request)
        await store.append(session_id, JobStartedEvent(
            event_id=create_prefixed_id("evt"), job_id=request.job_id,
            timestamp=datetime.now(UTC),
            payload=JobStartedPayload(session_id=session_id, thread_id=thread_id),
        ))
        queued = _created_request(session_id, thread_id, f"job-queued-request-{index}")
        queued.payload.message = "排队请求正文" * 200_000
        await store.append(session_id, queued)
    payloads: dict[str, list[weakref.ReferenceType[JobCreatedPayload]]] = {}
    original_read = store.read_events

    def observed_read(session_id: str):
        events = original_read(session_id)
        payloads.setdefault(session_id, [
            weakref.ref(event.payload) for event in events if event.type == "job_created"
        ])
        return events

    async def observed_restart(job: JobState) -> bool:
        # 恢复任何会话前已经核完全部 owner，且不再持有其他会话的预校验正文。
        assert set(payloads) == set(session_ids)
        assert all(
            reference() is None for owner, references in payloads.items()
            if owner != job.session_id for reference in references
        )
        return await before_provider(job)

    monkeypatch.setattr(store, "read_events", observed_read)
    monkeypatch.setattr(runtime, "_can_restart_before_provider", observed_restart)

    assert await runtime.reconcile_stale_executions() == 0

    assert len(calls) == 4
    assert len(jobs._jobs) == 2
