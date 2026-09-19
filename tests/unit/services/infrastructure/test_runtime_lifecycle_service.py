from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.schemas.event import (
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
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.trace_event_store import TraceEventStore


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

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
    ) -> bool:
        self.calls.append(
            {"session_id": session_id, "turn_id": turn_id, "status": status}
        )
        return True


def build_runtime(
    tmp_path: Path,
    terminal_status_writer: RecordingTurnStatusWriter | None = None,
) -> tuple[RuntimeService, JobService]:
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    bus = RecordingJobEventBus()
    jobs = JobService(
        job_event_bus=bus,
        job_executor=NeverFinishExecutor(),
    )
    runtime = RuntimeService(
        workspace_id="00000000-0000-4000-8000-000000000001",
        workspace_root=tmp_path,
        job_service=jobs,
        background_task_registry=BackgroundTaskRegistry(
            history_store=BackgroundTaskHistoryStore(sessions_dir=sessions_dir)
        ),
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
async def test_status_reports_the_injected_workspace_uuid_and_root(tmp_path: Path) -> None:
    runtime, _ = build_runtime(tmp_path)

    status = await runtime.status()

    assert status.workspace_id == "00000000-0000-4000-8000-000000000001"
    assert status.storage.root == str(tmp_path)
    assert status.storage.log_dir == str(tmp_path / ".boxteam" / "logs")


@pytest.mark.asyncio
async def test_drain_closes_admission_and_cancel_reopens_it(tmp_path: Path) -> None:
    runtime, jobs = build_runtime(tmp_path)

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
async def test_force_interrupt_persists_event_and_cancels_job(tmp_path: Path) -> None:
    runtime, jobs = build_runtime(tmp_path)
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
    )
    jobs._jobs[job.job_id] = job
    jobs._session_current_job[job.session_id] = job.job_id

    draining = await runtime.begin_drain()
    forced = await runtime.force_interrupt()

    assert [blocker.resource_id for blocker in draining.blockers] == ["job_running"]
    assert forced.lifecycle_state == "stopping"
    assert forced.interrupted_resources == 1
    assert task.cancelled()
    assert job.status == JobStatus.cancelled
    assert job.error_message == "Gateway 显式强制重启 Workspace API"


@pytest.mark.asyncio
async def test_startup_reconciles_job_without_terminal_event(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_29e5b12a664c4bad8baaf88f2b34a3ab")
    runtime, _ = build_runtime(tmp_path)
    store = runtime._trace_event_store
    now = datetime.now(UTC)
    await store.append(
        "ses_29e5b12a664c4bad8baaf88f2b34a3ab",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=now,
            payload=JobStartedPayload(),
        ),
    )

    reconciled = await runtime.reconcile_stale_executions()
    events = store.read_events("ses_29e5b12a664c4bad8baaf88f2b34a3ab")

    assert reconciled == 1
    assert events[-1].type == "session_interrupted"
    assert events[-1].payload.phase == "process_exit"


@pytest.mark.asyncio
async def test_startup_keeps_session_with_invalid_trace_available(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_root = session_bundle_factory(
        tmp_path / ".boxteam" / "sessions", "ses_6cf12b207df7421385418036cbcd9a27"
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
    runtime, _ = build_runtime(tmp_path)

    assert await runtime.reconcile_stale_executions() == 0
    assert runtime.startup_reconciliation_errors == [
        {
            "session_id": "ses_6cf12b207df7421385418036cbcd9a27",
            "error": "Trace 事件协议无效: session_id=ses_6cf12b207df7421385418036cbcd9a27 event="
            "{'event_id': 'evt_legacy_text_start', 'job_id': 'job_legacy', "
            "'step_id': None, 'agent_id': 'default', "
            "'timestamp': '2026-07-05T14:51:24.999582+00:00', "
            "'type': 'text_start', 'payload': {}}",
        }
    ]


@pytest.mark.asyncio
async def test_startup_reconciles_persisted_turn_as_failed(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_29e5b12a664c4bad8baaf88f2b34a3ab")
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(tmp_path, writer)
    await runtime._trace_event_store.append(
        "ses_29e5b12a664c4bad8baaf88f2b34a3ab",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=datetime.now(UTC),
            payload=JobStartedPayload(),
        ),
    )

    await runtime.reconcile_stale_executions()

    assert writer.calls == [
        {"session_id": "ses_29e5b12a664c4bad8baaf88f2b34a3ab", "turn_id": "job_stale", "status": "failed"}
    ]
    event = runtime._trace_event_store.read_events("ses_29e5b12a664c4bad8baaf88f2b34a3ab")[-1]
    assert event.payload.code == "execution_lost"
    assert event.payload.resumable is False


@pytest.mark.asyncio
async def test_startup_repairs_existing_process_exit_turn_status(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_29e5b12a664c4bad8baaf88f2b34a3ab")
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(tmp_path, writer)
    now = datetime.now(UTC)
    await runtime._trace_event_store.append(
        "ses_29e5b12a664c4bad8baaf88f2b34a3ab",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=now,
            payload=JobStartedPayload(),
        ),
    )
    await runtime._trace_event_store.append(
        "ses_29e5b12a664c4bad8baaf88f2b34a3ab",
        SessionInterruptedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=now,
            payload=SessionInterruptedPayload(
                session_id="ses_29e5b12a664c4bad8baaf88f2b34a3ab",
                phase="process_exit",
                code="execution_lost",
                message="工作区后端重启，无法安全续接原 AgentLoop 执行",
            ),
        ),
    )

    reconciled = await runtime.reconcile_stale_executions()

    assert reconciled == 0
    assert writer.calls == [
        {"session_id": "ses_29e5b12a664c4bad8baaf88f2b34a3ab", "turn_id": "job_stale", "status": "failed"}
    ]
    assert len(runtime._trace_event_store.read_events("ses_29e5b12a664c4bad8baaf88f2b34a3ab")) == 2


@pytest.mark.asyncio
async def test_startup_preserves_timeout_status_from_failed_event(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_e4a9cc5baba448238041d63a5cb89e05")
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(tmp_path, writer)
    await runtime._trace_event_store.append(
        "ses_e4a9cc5baba448238041d63a5cb89e05",
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

    assert writer.calls == [{
        "session_id": "ses_e4a9cc5baba448238041d63a5cb89e05",
        "turn_id": "job_timeout",
        "status": "timed_out",
    }]


@pytest.mark.asyncio
async def test_startup_preserves_legacy_timeout_text_status(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_ef850e62581f46b88eef10234b31bfd7")
    writer = RecordingTurnStatusWriter()
    runtime, _ = build_runtime(tmp_path, writer)
    await runtime._trace_event_store.append(
        "ses_ef850e62581f46b88eef10234b31bfd7",
        JobFailedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_legacy_timeout",
            timestamp=datetime.now(UTC),
            payload=JobFailedPayload(error="Job 执行超过总超时上限"),
        ),
    )

    await runtime.reconcile_stale_executions()

    assert writer.calls == [{
        "session_id": "ses_ef850e62581f46b88eef10234b31bfd7",
        "turn_id": "job_legacy_timeout",
        "status": "timed_out",
    }]
