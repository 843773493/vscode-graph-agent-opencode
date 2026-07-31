import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.abstractions.turn_history import (
    TurnBootstrapBatch,
    TurnIndexedEvent,
    TurnMigrationSnapshot,
    TurnRecoveryBatch,
)
from app.schemas.event import (
    Event,
    JobCompletedEvent,
    JobCompletedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
)
from app.schemas.public_v2.common import JobStatus, RunMode
from app.schemas.public_v2.job import JobDTO
from app.schemas.public_v2.message import MessageDTO
from app.schemas.public_v2.pending_request import (
    PendingRequestDTO,
    PendingRequestListDTO,
    PendingRequestSummaryDTO,
    PendingRequestSummaryListDTO,
)
from app.schemas.public_v2.session import SessionDTO
from app.services.business.session_turn_history import (
    SessionTurnHistoryMigrator,
    SessionTurnHistoryService,
    TurnHistoryProjector,
)
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.infrastructure.turn_history import TurnHistoryStore


class _Sessions:
    async def get(self, session_id: str) -> SessionDTO:
        now = datetime.now(UTC)
        return SessionDTO(
            session_id=session_id,
            workspace_id="workspace_1",
            title="长会话",
            current_agent_id="default",
            created_at=now,
            updated_at=now,
        )


class _LegacyMessages:
    def __init__(self, messages: list[MessageDTO] | None = None) -> None:
        self.messages = messages or []

    def has_checkpoint_history(self, session_id: str) -> bool:
        return bool(self.messages)

    async def list_visible_messages_for_turn_migration(
        self,
        session_id: str,
    ) -> list[MessageDTO]:
        return self.messages


class _Jobs:
    def __init__(self, count: int = 0) -> None:
        now = datetime.now(UTC)
        self.list_called = False
        self.active = (
            JobDTO(
                job_id="active_job_0",
                message_id="active_message_0",
                session_id="session_1",
                mode=RunMode.single_agent,
                status=JobStatus.running,
                entry_agent="default",
                created_at=now,
                updated_at=now,
            )
            if count
            else None
        )
        self.requests = [
            PendingRequestDTO(
                job_id=f"pending_job_{index}",
                message_id=f"pending_message_{index}",
                session_id="session_1",
                content="pending",
                kind="queued",
                position=index,
                agent_id="default",
                message_created_at=now.isoformat(),
                created_at=now,
                updated_at=now + timedelta(seconds=index),
            )
            for index in range(max(0, count - 1))
        ]

    async def list(self, session_id: str | None = None) -> list[JobDTO]:
        self.list_called = True
        raise AssertionError("bootstrap 不得扫描完整 Job 历史")

    async def get(self, job_id: str) -> JobDTO:
        if self.active is None or self.active.job_id != job_id:
            raise KeyError(job_id)
        return self.active

    async def list_pending(self, session_id: str) -> PendingRequestListDTO:
        raise AssertionError("bootstrap 不得加载完整 pending request")

    async def list_pending_summaries(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> PendingRequestSummaryListDTO:
        summaries = [
            PendingRequestSummaryDTO(
                job_id=request.job_id,
                message_id=request.message_id,
                updated_at=request.updated_at,
            )
            for request in self.requests[:limit]
        ]
        return PendingRequestSummaryListDTO(
            session_id=session_id,
            active_job_id=self.active.job_id if self.active is not None else None,
            requests=summaries,
            request_count=len(self.requests),
            truncated=len(summaries) < len(self.requests),
        )


class _TraceEvents:
    def __init__(self, events: list[Event]) -> None:
        self.events = events
        self.calls: list[tuple[str, int | None]] = []

    def ensure_turn_index(self, session_id: str) -> None:
        return None

    def read_message_events(
        self,
        session_id: str,
        tail_limit: int | None = None,
    ) -> list[Event]:
        self.calls.append(("messages", tail_limit))
        return self.events if tail_limit is None else self.events[-tail_limit:]

    def read_turn_bootstrap_batch(
        self,
        session_id: str,
        *,
        max_events: int,
        max_bytes: int,
    ) -> TurnBootstrapBatch:
        self.calls.append(("bootstrap", max_events))
        latest_created_index = next(
            (
                index
                for index in range(len(self.events) - 1, -1, -1)
                if self.events[index].type == "job_created"
            ),
            None,
        )
        if latest_created_index is None:
            selected: list[tuple[int, Event]] = []
            omitted_latest_job_events = False
        else:
            latest_job_id = self.events[latest_created_index].job_id
            job_events = [
                (index, event)
                for index, event in enumerate(self.events[latest_created_index:])
                if event.job_id == latest_job_id
            ]
            selected = (
                [job_events[0], *job_events[-(max_events - 1) :]]
                if len(job_events) > max_events
                else job_events
            )
            omitted_latest_job_events = len(job_events) > max_events
            selected = [
                (latest_created_index + index, event) for index, event in selected
            ]
        return TurnBootstrapBatch(
            events=[
                TurnIndexedEvent(event=event, source_offset=index + 1)
                for index, event in selected
            ],
            event_cursor=self.events[-1].event_id if self.events else None,
            event_offset=len(self.events) if self.events else None,
            has_older_events=(
                bool(self.events)
                if latest_created_index is None
                else latest_created_index > 0 or omitted_latest_job_events
            ),
        )

    def read_turn_recovery_batch(
        self,
        session_id: str,
        *,
        after_event_id: str | None,
        max_events: int,
        max_bytes: int,
    ) -> TurnRecoveryBatch:
        self.calls.append(("recovery", max_events))
        if after_event_id is None:
            return TurnRecoveryBatch(
                event_cursor=self.events[-1].event_id if self.events else None,
                event_offset=len(self.events) if self.events else None,
                complete=not self.events,
            )
        cursor_index = next(
            (
                index
                for index, event in enumerate(self.events)
                if event.event_id == after_event_id
            ),
            None,
        )
        if cursor_index is None:
            return TurnRecoveryBatch(complete=False)
        pending = self.events[cursor_index + 1 :]
        return TurnRecoveryBatch(
            events=[
                TurnIndexedEvent(event=event, source_offset=index + 1)
                for index, event in enumerate(pending, start=cursor_index + 1)
            ],
            event_cursor=self.events[-1].event_id if self.events else None,
            event_offset=len(self.events) if self.events else None,
            complete=len(pending) <= max_events,
        )

    def read_events(
        self,
        session_id: str,
        after_event_id: str | None = None,
        tail_limit: int | None = None,
    ) -> list[Event]:
        self.calls.append(("events", tail_limit))
        return self.events if tail_limit is None else self.events[-tail_limit:]

    def capture_turn_migration_snapshot(
        self,
        session_id: str,
    ) -> TurnMigrationSnapshot:
        self.calls.append(("snapshot", None))
        return TurnMigrationSnapshot(
            message_trace_size=len(self.events),
            event_cursor=self.events[-1].event_id if self.events else None,
            projected_event_offset=len(self.events) if self.events else None,
        )

    def iter_message_events(
        self,
        session_id: str,
        *,
        before_offset: int | None = None,
    ):
        self.calls.append(("iter", before_offset))
        end = len(self.events) if before_offset is None else before_offset
        yield from list(self.events[:end])


def _event(
    index: int,
    *,
    content_size: int = 1,
    include_inline_media: bool = True,
) -> JobCreatedEvent:
    created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    return JobCreatedEvent(
        event_id=f"event_{index}",
        job_id=f"job_{index}",
        timestamp=created_at,
        payload=JobCreatedPayload(
            session_id="session_1",
            message="x" * content_size,
            agent_id="default",
            message_id=f"message_{index}",
            message_created_at=created_at,
            message_metadata=(
                {"inline_data_url": "data:image/png;base64," + "A" * 100_000}
                if include_inline_media
                else {}
            ),
        ),
    )


def _completed(index: int) -> JobCompletedEvent:
    return JobCompletedEvent(
        event_id=f"event_{index}_completed",
        job_id=f"job_{index}",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        payload=JobCompletedPayload(result=f"result_{index}"),
    )


def _build_service(
    sessions_dir: Path,
    *,
    store: TurnHistoryStore,
    trace: _TraceEvents | TraceEventStore,
    projector: TurnHistoryProjector | None = None,
    legacy_source: _LegacyMessages | None = None,
    jobs: _Jobs | None = None,
) -> SessionTurnHistoryService:
    resolved_projector = projector or TurnHistoryProjector(store)
    resolved_legacy_source = legacy_source or _LegacyMessages()

    def staging_store_factory() -> TurnHistoryStore:
        return TurnHistoryStore(
            sessions_dir,
            directory_name="turn_history_staging",
            write_durability="publish",
        )

    migrator = SessionTurnHistoryMigrator(
        store=store,
        trace_event_store=trace,
        legacy_message_source=resolved_legacy_source,
        staging_store_factory=staging_store_factory,
        projector_factory=TurnHistoryProjector,
    )
    return SessionTurnHistoryService(
        store=store,
        projector=resolved_projector,
        trace_event_store=trace,
        session_service=_Sessions(),  # type: ignore[arg-type]
        job_service=jobs or _Jobs(),  # type: ignore[arg-type]
        migrator=migrator,
    )


@pytest.fixture
def history_service(
    tmp_path: Path,
    session_bundle_factory,
) -> tuple[SessionTurnHistoryService, TurnHistoryStore, _TraceEvents]:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    projector = TurnHistoryProjector(store)
    trace = _TraceEvents([_event(1), _event(2, content_size=100_000)])
    service = _build_service(
        sessions_dir,
        store=store,
        projector=projector,
        trace=trace,
        jobs=_Jobs(10),
    )
    return service, store, trace


@pytest.mark.asyncio
async def test_bootstrap_only_reads_bounded_tail_and_latest_turn(
    history_service: tuple[SessionTurnHistoryService, TurnHistoryStore, _TraceEvents],
) -> None:
    service, store, trace = history_service

    result, needs_completion = await service.bootstrap("session_1")

    assert needs_completion is True
    assert result.projection_state == "partial"
    assert result.latest_turn is not None
    assert result.latest_turn.turn_id == "job_2"
    assert len(result.latest_turn.user_messages[0].preview) == 500
    assert "data:image" not in result.model_dump_json()
    assert result.active_job_count == 10
    assert result.active_job_id == "active_job_0"
    assert len(result.active_jobs) == 8
    assert result.active_jobs_truncated is True
    assert result.event_cursor == "event_2"
    assert trace.calls == [("bootstrap", 128)]
    assert store.turn_count("session_1") == 1


@pytest.mark.asyncio
async def test_ready_bootstrap_does_not_read_full_turn_record(
    history_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, _ = history_service
    _, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    await service.complete_migration("session_1")

    def reject_full_record(*args, **kwargs):
        raise AssertionError("bootstrap 不得读取完整 Turn detail")

    monkeypatch.setattr(store._files, "read_turn_record", reject_full_record)
    result, needs_completion = await service.bootstrap("session_1")

    assert needs_completion is False
    assert result.latest_turn is not None
    assert result.latest_turn.turn_id == "job_2"
    assert store.turn_count("session_1") == 2


@pytest.mark.asyncio
async def test_empty_new_session_bootstrap_does_not_scan_unbounded_trace(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    trace = _TraceEvents([])
    service = _build_service(sessions_dir, store=store, trace=trace)

    result, needs_completion = await service.bootstrap("session_1")

    assert needs_completion is False
    assert result.latest_turn is None
    assert result.projection_state == "ready"
    assert trace.calls == [("bootstrap", 128)]


@pytest.mark.asyncio
async def test_recent_projection_does_not_create_phantom_turn_from_mid_job_tail(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    trace = _TraceEvents([_completed(1)])
    service = _build_service(sessions_dir, store=store, trace=trace)

    result, needs_completion = await service.bootstrap("session_1")

    assert needs_completion is True
    assert result.projection_state == "partial"
    assert result.latest_turn is None
    assert store.turn_count("session_1") == 0


@pytest.mark.asyncio
async def test_recent_projection_locates_job_start_for_single_turn_over_128_events(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    created = _event(1)
    terminal = _completed(1)
    events: list[Event] = [
        created,
        *[
            terminal.model_copy(
                update={
                    "event_id": f"event_long_terminal_{index:03d}",
                    "timestamp": terminal.timestamp + timedelta(milliseconds=index),
                }
            )
            for index in range(140)
        ],
    ]
    trace = _TraceEvents(events)
    service = _build_service(sessions_dir, store=store, trace=trace)

    result, needs_completion = await service.bootstrap("session_1")

    assert needs_completion is True
    assert result.latest_turn is not None
    assert result.latest_turn.turn_id == created.job_id
    assert result.latest_turn.status == JobStatus.completed


@pytest.mark.asyncio
async def test_bootstrap_and_live_projection_share_cursor_critical_section(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    class BootstrapBarrierTrace(_TraceEvents):
        def __init__(self, events: list[Event]) -> None:
            super().__init__(events)
            self.captured = threading.Event()
            self.release = threading.Event()

        def read_turn_bootstrap_batch(
            self,
            session_id: str,
            *,
            max_events: int,
            max_bytes: int,
        ) -> TurnBootstrapBatch:
            batch = super().read_turn_bootstrap_batch(
                session_id,
                max_events=max_events,
                max_bytes=max_bytes,
            )
            self.captured.set()
            self.release.wait(timeout=5)
            return batch

    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    projector = TurnHistoryProjector(store)
    created = _event(2)
    trace = BootstrapBarrierTrace([created])
    service = _build_service(
        sessions_dir,
        store=store,
        projector=projector,
        trace=trace,
    )

    bootstrap_task = asyncio.create_task(service.bootstrap("session_1"))
    assert await asyncio.to_thread(trace.captured.wait, 2)
    completed = _completed(2)
    trace.events.append(completed)
    live_task = asyncio.create_task(
        asyncio.to_thread(
            projector.record_event,
            "session_1",
            completed,
            source_offset=2,
        )
    )
    await asyncio.sleep(0.05)
    assert live_task.done() is False
    trace.release.set()
    await bootstrap_task
    await live_task

    detail = store.get_turn("session_1", "job_2")
    assert detail is not None
    assert detail.revision == 2
    assert detail.status == JobStatus.completed
    assert store.event_cursor("session_1") == completed.event_id


@pytest.mark.asyncio
async def test_bootstrap_replays_trace_events_committed_before_turn_projection(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    trace = TraceEventStore(sessions_dir)
    store = TurnHistoryStore(sessions_dir)
    projector = TurnHistoryProjector(store)
    service = _build_service(
        sessions_dir,
        store=store,
        projector=projector,
        trace=trace,
    )
    created = _event(1, include_inline_media=False)
    await trace.append("session_1", created)
    first, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is False
    assert first.latest_turn is not None
    assert store.get_turn("session_1", created.job_id).revision == 1

    completed = _completed(1)
    await trace.append("session_1", completed)
    second, needs_completion = await service.bootstrap("session_1")

    assert needs_completion is False
    assert second.latest_turn is not None
    detail = store.get_turn("session_1", created.job_id)
    assert detail is not None
    assert detail.revision == 2
    assert detail.status == JobStatus.completed
    assert store.event_cursor("session_1") == completed.event_id

