from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.event import Event
from app.schemas.public_v2.common import JobStatus, MessageRole
from app.schemas.public_v2.message import MessageDTO
from app.services.business.session_turn_history import (
    SessionTurnHistoryService,
    TurnHistoryProjector,
)
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.infrastructure.turn_history import TurnHistoryStore
from tests.unit.services.business.test_session_turn_history_service import (
    _build_service,
    _completed,
    _event,
    _LegacyMessages,
    _TraceEvents,
)


@pytest.fixture
def migration_history_service(
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
    )
    return service, store, trace


@pytest.mark.asyncio
async def test_complete_migration_rebuilds_full_history_and_increments_epoch(
    migration_history_service: tuple[
        SessionTurnHistoryService,
        TurnHistoryStore,
        _TraceEvents,
    ],
) -> None:
    service, store, trace = migration_history_service
    result, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    old_epoch = result.projection_epoch

    await service.complete_migration("session_1")

    page, needs_completion = await service.list_turns(
        "session_1",
        limit=20,
        cursor=None,
    )
    assert needs_completion is False
    assert [turn.turn_id for turn in page.items] == ["job_2", "job_1"]
    assert page.projection_epoch == old_epoch + 1
    assert store.projection_status("session_1") == "ready"
    assert ("snapshot", None) in trace.calls
    assert ("iter", 2) in trace.calls


@pytest.mark.asyncio
async def test_checkpoint_legacy_migration_keeps_history_when_new_trace_arrives(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    legacy_time = datetime(2025, 12, 31, tzinfo=UTC)
    legacy_source = _LegacyMessages(
        [
            MessageDTO(
                message_id="legacy_user_1",
                session_id="session_1",
                role=MessageRole.user,
                content="旧问题",
                attachments=[],
                metadata={},
                created_at=legacy_time,
                updated_at=legacy_time,
            ),
            MessageDTO(
                message_id="legacy_assistant_1",
                session_id="session_1",
                role=MessageRole.assistant,
                content="旧回答",
                attachments=[],
                metadata={},
                created_at=legacy_time + timedelta(seconds=1),
                updated_at=legacy_time + timedelta(seconds=1),
            ),
        ]
    )
    store = TurnHistoryStore(sessions_dir)
    trace = _TraceEvents([])
    service = _build_service(
        sessions_dir,
        store=store,
        trace=trace,
        legacy_source=legacy_source,
    )

    initial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    assert initial.projection_state == "partial"
    assert initial.latest_turn is None

    trace.events.extend([_event(2, include_inline_media=False), _completed(2)])
    await service.complete_migration("session_1")

    page, needs_completion = await service.list_turns(
        "session_1",
        limit=20,
        cursor=None,
    )
    assert needs_completion is False
    assert [turn.turn_id for turn in page.items] == [
        "job_2",
        "legacy:legacy_user_1",
    ]
    details, _ = await service.get_details(
        "session_1",
        ["legacy:legacy_user_1", "job_2"],
    )
    assert [turn.final_response for turn in details.items] == [
        "旧回答",
        "result_2",
    ]


@pytest.mark.asyncio
async def test_checkpoint_only_migration_stays_ready_without_trace_cursor(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    legacy_time = datetime(2025, 12, 31, tzinfo=UTC)
    legacy_source = _LegacyMessages(
        [
            MessageDTO(
                message_id="legacy_user_only",
                session_id="session_1",
                role=MessageRole.user,
                content="只有 checkpoint 的旧问题",
                attachments=[],
                metadata={},
                created_at=legacy_time,
                updated_at=legacy_time,
            ),
            MessageDTO(
                message_id="legacy_assistant_only",
                session_id="session_1",
                role=MessageRole.assistant,
                content="只有 checkpoint 的旧回答",
                attachments=[],
                metadata={},
                created_at=legacy_time + timedelta(seconds=1),
                updated_at=legacy_time + timedelta(seconds=1),
            ),
        ]
    )
    store = TurnHistoryStore(sessions_dir)
    trace = TraceEventStore(sessions_dir)
    service = _build_service(
        sessions_dir,
        store=store,
        trace=trace,
        legacy_source=legacy_source,
    )

    initial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    assert initial.projection_state == "partial"
    assert initial.latest_turn is None

    await service.complete_migration("session_1")
    first_ready, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is False
    assert first_ready.projection_state == "ready"
    assert first_ready.event_cursor is None
    assert first_ready.latest_turn is not None
    assert first_ready.latest_turn.turn_id == "legacy:legacy_user_only"
    ready_epoch = first_ready.projection_epoch

    second_ready, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is False
    assert second_ready.projection_state == "ready"
    assert second_ready.projection_epoch == ready_epoch
    assert second_ready.latest_turn == first_ready.latest_turn


@pytest.mark.asyncio
async def test_checkpoint_race_deduplicates_new_trace_by_job_identity(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    legacy_time = datetime(2025, 12, 31, tzinfo=UTC)
    legacy_source = _LegacyMessages(
        [
            MessageDTO(
                message_id="legacy_unique_user",
                session_id="session_1",
                role=MessageRole.user,
                content="需要保留的旧问题",
                attachments=[],
                metadata={},
                created_at=legacy_time,
                updated_at=legacy_time,
            ),
            MessageDTO(
                message_id="legacy_unique_assistant",
                session_id="session_1",
                role=MessageRole.assistant,
                content="需要保留的旧回答",
                attachments=[],
                metadata={},
                created_at=legacy_time + timedelta(seconds=1),
                updated_at=legacy_time + timedelta(seconds=1),
            ),
            MessageDTO(
                message_id="checkpoint_user_with_different_id",
                session_id="session_1",
                role=MessageRole.user,
                content="已经由新 Trace 表示的问题",
                attachments=[],
                metadata={"job_id": "job_2"},
                created_at=legacy_time + timedelta(days=1),
                updated_at=legacy_time + timedelta(days=1),
            ),
            MessageDTO(
                message_id="checkpoint_assistant_with_different_id",
                session_id="session_1",
                role=MessageRole.assistant,
                content="不应生成重复 Turn",
                attachments=[],
                metadata={},
                created_at=legacy_time + timedelta(days=1, seconds=1),
                updated_at=legacy_time + timedelta(days=1, seconds=1),
            ),
        ]
    )
    store = TurnHistoryStore(sessions_dir)
    trace = _TraceEvents([])
    service = _build_service(
        sessions_dir,
        store=store,
        trace=trace,
        legacy_source=legacy_source,
    )

    initial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    assert initial.projection_state == "partial"

    trace.events.extend([_event(2, include_inline_media=False), _completed(2)])
    await service.complete_migration("session_1")
    page, needs_completion = await service.list_turns(
        "session_1",
        limit=20,
        cursor=None,
    )

    assert needs_completion is False
    assert [turn.turn_id for turn in page.items] == [
        "job_2",
        "legacy:legacy_unique_user",
    ]
    assert store.turn_count("session_1") == 2


@pytest.mark.asyncio
async def test_migration_keeps_latest_snapshot_readable_until_atomic_publish(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    class BarrierTrace(_TraceEvents):
        def __init__(self, events: list[Event]) -> None:
            super().__init__(events)
            self.started = threading.Event()
            self.release = threading.Event()

        def iter_message_events(
            self,
            session_id: str,
            *,
            before_offset: int | None = None,
        ):
            self.calls.append(("iter", before_offset))
            self.started.set()
            self.release.wait(timeout=5)
            end = len(self.events) if before_offset is None else before_offset
            yield from list(self.events[:end])

    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    trace = BarrierTrace([_event(1), _event(2)])
    service = _build_service(sessions_dir, store=store, trace=trace)
    initial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    assert initial.latest_turn is not None
    old_epoch = initial.projection_epoch

    migration = asyncio.create_task(service.complete_migration("session_1"))
    assert await asyncio.to_thread(trace.started.wait, 2)
    during_details, _ = await service.get_details(
        "session_1",
        [initial.latest_turn.turn_id],
    )
    during_bootstrap, still_partial = await service.bootstrap("session_1")

    assert during_details.items[0].turn_id == initial.latest_turn.turn_id
    assert during_bootstrap.latest_turn == initial.latest_turn
    assert during_bootstrap.projection_epoch == old_epoch
    assert during_bootstrap.projection_state == "partial"
    assert still_partial is True

    trace.release.set()
    await migration
    completed, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is False
    assert completed.projection_state == "ready"
    assert completed.projection_epoch == old_epoch + 1
    assert completed.latest_turn is not None
    assert completed.latest_turn.turn_id == initial.latest_turn.turn_id


@pytest.mark.asyncio
async def test_live_projection_before_bootstrap_does_not_skip_older_trace(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    projector = TurnHistoryProjector(store)
    trace = _TraceEvents([_event(1), _event(2)])
    service = _build_service(
        sessions_dir,
        store=store,
        projector=projector,
        trace=trace,
    )
    projector.record_event("session_1", trace.events[-1], source_offset=2)
    assert store.history_initialized("session_1") is False

    initial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    assert initial.latest_turn is not None
    assert initial.latest_turn.turn_id == "job_2"

    await service.complete_migration("session_1")
    page, needs_completion = await service.list_turns(
        "session_1",
        limit=20,
        cursor=None,
    )
    assert needs_completion is False
    assert [item.turn_id for item in page.items] == ["job_2", "job_1"]


@pytest.mark.asyncio
async def test_migration_snapshot_conflict_never_loses_live_turn(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    class EofBarrierTrace(_TraceEvents):
        def __init__(self, events: list[Event]) -> None:
            super().__init__(events)
            self.at_eof = threading.Event()
            self.release = threading.Event()

        def iter_message_events(
            self,
            session_id: str,
            *,
            before_offset: int | None = None,
        ):
            self.calls.append(("iter", before_offset))
            end = len(self.events) if before_offset is None else before_offset
            yield from list(self.events[:end])
            self.at_eof.set()
            self.release.wait(timeout=5)

    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    projector = TurnHistoryProjector(store)
    trace = EofBarrierTrace([_event(1), _event(2)])
    service = _build_service(
        sessions_dir,
        store=store,
        projector=projector,
        trace=trace,
    )
    initial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    old_epoch = initial.projection_epoch

    migration = asyncio.create_task(service.complete_migration("session_1"))
    assert await asyncio.to_thread(trace.at_eof.wait, 2)
    live_created = _event(3)
    live_completed = _completed(3)
    trace.events.extend([live_created, live_completed])
    projector.record_event("session_1", live_created, source_offset=3)
    projector.record_event("session_1", live_completed, source_offset=4)
    trace.release.set()
    await migration

    after_conflict, still_partial = await service.bootstrap("session_1")
    assert still_partial is True
    assert after_conflict.projection_epoch == old_epoch
    assert after_conflict.latest_turn is not None
    assert after_conflict.latest_turn.turn_id == "job_3"
    assert after_conflict.latest_turn.status == JobStatus.completed

    await service.complete_migration("session_1")
    completed, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is False
    assert completed.projection_epoch == old_epoch + 1
    assert completed.latest_turn is not None
    assert completed.latest_turn.turn_id == "job_3"
    page, _ = await service.list_turns("session_1", limit=20, cursor=None)
    assert [item.turn_id for item in page.items] == ["job_3", "job_2", "job_1"]


@pytest.mark.asyncio
async def test_failed_migration_stays_failed_and_bootstrap_exposes_error(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    class CorruptedTrace(_TraceEvents):
        def iter_message_events(
            self,
            session_id: str,
            *,
            before_offset: int | None = None,
        ):
            raise RuntimeError("messages.jsonl 第 7 行损坏")
            yield  # pragma: no cover

    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    trace = CorruptedTrace([_event(1), _event(2)])
    service = _build_service(sessions_dir, store=store, trace=trace)
    _, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True

    with pytest.raises(RuntimeError, match="messages.jsonl 第 7 行损坏"):
        await service.complete_migration("session_1")

    with pytest.raises(
        RuntimeError,
        match="Turn 投影处于失败状态.*messages.jsonl 第 7 行损坏",
    ):
        await service.bootstrap("session_1")


@pytest.mark.asyncio
async def test_ready_legacy_projection_without_index_migrates_trace_gap_once(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_dir = session_bundle_factory(sessions_dir, "session_1")
    created = _event(1)
    completed = _completed(1)
    lines = [
        event.model_dump_json().encode("utf-8") + b"\n"
        for event in (created, completed)
    ]
    trace_dir = session_dir / "logs" / "traces"
    trace_dir.mkdir(parents=True)
    (trace_dir / "events.jsonl").write_bytes(b"".join(lines))
    (trace_dir / "messages.jsonl").write_bytes(b"".join(lines))

    store = TurnHistoryStore(sessions_dir)
    projector = TurnHistoryProjector(store)
    projector.record_event("session_1", created, source_offset=len(lines[0]))
    store.set_projection_status("session_1", "ready")
    store.mark_history_initialized("session_1")
    trace = TraceEventStore(sessions_dir)
    service = _build_service(
        sessions_dir,
        store=store,
        projector=projector,
        trace=trace,
    )

    partial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    assert partial.projection_state == "partial"
    assert partial.latest_turn is not None
    assert partial.latest_turn.status == JobStatus.accepted

    await service.complete_migration("session_1")
    ready, needs_completion = await service.bootstrap("session_1")

    assert needs_completion is False
    assert ready.projection_state == "ready"
    assert ready.latest_turn is not None
    assert ready.latest_turn.status == JobStatus.completed
    assert ready.event_cursor == completed.event_id
    assert (trace_dir / "turn-events.index.json").is_file()
