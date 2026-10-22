from __future__ import annotations

import asyncio
import json
import weakref
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver, workspace_context
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    ExecutionAdmissionCursor,
    UserExecutionIdentity,
)
from app.domain.itemized.execution_queue import (
    ExecutionQueueInput,
    ExecutionQueueRecord,
)
from app.schemas.event import (
    JobCreatedEvent,
    JobCreatedPayload,
    JobFailedEvent,
    JobFailedPayload,
    JobStartedEvent,
    JobStartedPayload,
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


class EmptyPendingRequestStore:
    """Runtime 协议替身；只暴露测试显式登记的持久 admission。"""

    def __init__(self) -> None:
        self._records: list[ExecutionQueueRecord] = []
        self._inputs: dict[str, object] = {}

    def add_started(self, event: JobCreatedEvent) -> None:
        payload = event.payload
        binding = payload.execution_binding
        assert isinstance(binding, UserExecutionIdentity)
        created_at = payload.message_created_at.isoformat()
        queue_input = ExecutionQueueInput(
            execution_binding=binding,
            message_id=payload.message_id,
            agent_id=payload.agent_id,
            provider_id=None,
            delivery_policy="after_turn",
            gateway_id=None,
            request_id=None,
            created_at=created_at,
        )
        admission = ExecutionAdmission(
            session_id=payload.session_id,
            thread_id=payload.thread_id,
            execution_id=f"execution-{event.job_id}",
            job_id=event.job_id,
            admission_idempotency_key=binding.admission_idempotency_key,
            execution_origin="user_turn",
            turn_id=binding.turn_id,
        )
        record = ExecutionQueueRecord(
            cursor=ExecutionAdmissionCursor(admission, len(self._records) + 1, "acceptance"),
            input=queue_input,
            delivery_policy="after_turn",
            phase="started",
            claim_owner="previous-runtime",
            claim_generation=1,
            updated_at=created_at,
            outcome="unknown",
        )
        self._records.append(record)
        self._inputs[event.job_id] = SimpleNamespace(
            content=payload.message,
            message_id=payload.message_id,
            message_created_at=created_at,
            execution_binding=binding,
            agent_id=payload.agent_id,
            message_metadata={},
            attachments=[],
            gateway_id=None,
            request_id=None,
            created_at=created_at,
            updated_at=created_at,
            delivery_policy="after_turn",
        )

    async def records(self):
        return tuple(self._records)

    async def claim(self, record):
        async def close():
            return None

        return SimpleNamespace(record=record, close=close)

    async def project(self, record, *, position: int, snapshot_version: int):
        return self._inputs[record.cursor.admission.job_id]

    async def before_dispatch(self, _record):
        return False

    def add(self, record: ExecutionQueueRecord, request: object) -> None:
        self._records.append(record)
        self._inputs[record.cursor.admission.job_id] = request


@pytest.fixture
def pending_checkpoint_inspector():
    async def inspect(_job: JobState) -> None:
        return None
    return inspect


def build_runtime(pending_checkpoint_inspector,
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
        background_task_registry=build_background_registry(sessions_dir),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        message_stream_store=MessageStreamStore(
            # R18 catalog 模式适配：经 path_utils 开关工厂取 resolver
            # （catalog 模式走 SQLite catalog 链，旧模式返回原 legacy
            # resolver，行为不变）。直连旧类会在 catalog 模式下解析不到
            # 经 factory 注册的会话。
            path_resolver=get_session_path_resolver(sessions_dir)
        ),
        pending_request_store=EmptyPendingRequestStore(),
        terminal_status_writer=terminal_status_writer,
    )
    return runtime, jobs


@pytest.mark.asyncio
async def test_status_reports_the_injected_workspace_uuid_without_storage_location(pending_checkpoint_inspector,
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime, _ = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        tmp_path=tmp_path, session_lifecycle_guard=session_lifecycle_guard
    )

    status = await runtime.status()

    assert status.workspace_id == "00000000-0000-4000-8000-000000000001"
    assert "storage" not in status.model_dump()
    assert str(tmp_path) not in status.model_dump_json()
    assert runtime.get_log_dir() == tmp_path / ".boxteam" / "logs"


@pytest.mark.asyncio
async def test_drain_closes_admission_and_cancel_reopens_it(pending_checkpoint_inspector,
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
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
async def test_force_interrupt_persists_event_and_cancels_job(pending_checkpoint_inspector,
    tmp_path: Path,
    session_lifecycle_guard,
) -> None:
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
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
async def test_startup_rejects_invalid_trace(pending_checkpoint_inspector,
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
        tmp_path=tmp_path, session_lifecycle_guard=session_lifecycle_guard
    )

    with pytest.raises(RuntimeError, match="Trace 事件协议无效"):
        await runtime.reconcile_stale_executions()


@pytest.mark.asyncio
async def test_terminal_trace_without_active_queue_admission_does_not_restore_or_rewrite_status(
    pending_checkpoint_inspector,
    tmp_path: Path,
    session_bundle_factory,
    session_lifecycle_guard,
) -> None:
    session_id = "ses_019c70f868da751480de63aa344e28c9"
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    writer = RecordingTurnStatusWriter()
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint_inspector,
        tmp_path=tmp_path,
        terminal_status_writer=writer,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    trace = runtime._trace_event_store
    thread_id = get_session_path_resolver(tmp_path / ".boxteam" / "sessions").main_thread_id(session_id)
    await trace.append(session_id, JobStartedEvent(
        event_id=create_prefixed_id("evt"), job_id="job-terminal", timestamp=datetime.now(UTC),
        payload=JobStartedPayload(session_id=session_id, thread_id=thread_id),
    ))
    await trace.append(session_id, JobFailedEvent(
        event_id=create_prefixed_id("evt"), job_id="job-terminal", timestamp=datetime.now(UTC),
        payload=JobFailedPayload(error="canonical terminal is already committed", code="job_timeout"),
    ))
    events_before = trace.read_events(session_id)

    assert await runtime.reconcile_stale_executions() == 0
    assert jobs._jobs == {}
    assert writer.calls == []
    assert trace.read_events(session_id) == events_before


@pytest.fixture
def recovery_inspectors():
    calls: list[tuple[str, JobState]] = []

    async def pending_checkpoint(job: JobState) -> str:
        calls.append(("pending_checkpoint", job))
        return "checkpoint-original"

    return calls, pending_checkpoint


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
    calls, pending_checkpoint = recovery_inspectors
    writer = RecordingTurnStatusWriter()
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint,
        tmp_path=tmp_path,
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
    runtime._pending_request_store.add_started(request)

    assert await runtime.reconcile_stale_executions() == 0

    assert [kind for kind, _ in calls] == ["pending_checkpoint"]
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
    calls, pending_checkpoint = recovery_inspectors
    writer = RecordingTurnStatusWriter()
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=pending_checkpoint,
        tmp_path=tmp_path,
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
    calls, _pending_checkpoint = recovery_inspectors
    runtime, jobs = build_runtime(
        pending_checkpoint_inspector=_pending_checkpoint,
        tmp_path=tmp_path,
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
        runtime._pending_request_store.add_started(request)
    payloads: dict[str, list[weakref.ReferenceType[JobCreatedPayload]]] = {}
    original_read = store.read_events

    def observed_read(session_id: str):
        events = original_read(session_id)
        payloads.setdefault(session_id, [
            weakref.ref(event.payload) for event in events if event.type == "job_created"
        ])
        return events

    async def observed_checkpoint(job: JobState) -> str:
        # 执行任何恢复回调前已校验所有 owner，并释放其他会话的 Trace 正文。
        assert set(payloads) == set(session_ids)
        assert all(
            reference() is None for owner, references in payloads.items()
            if owner != job.session_id for reference in references
        )
        calls.append(("pending_checkpoint", job))
        return "checkpoint-original"

    monkeypatch.setattr(store, "read_events", observed_read)
    monkeypatch.setattr(runtime, "_pending_tool_checkpoint_id", observed_checkpoint)

    assert await runtime.reconcile_stale_executions() == 0

    assert len(calls) == 2
    assert len(jobs._jobs) == 2
