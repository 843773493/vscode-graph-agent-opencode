from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.event import (
    Event,
    JobMergedEvent,
    JobMergedPayload,
    TextEndEvent,
    TextEndPayload,
)
from app.schemas.public_v2.common import JobStatus, MessageRole
from app.schemas.public_v2.message import MessageDTO
from app.services.infrastructure.turn_history import TurnHistoryStore
from tests.unit.services.business.test_session_turn_history_service import (
    _build_service,
    _completed,
    _event,
    _LegacyMessages,
    _TraceEvents,
)


def _checkpoint_pair(
    *,
    message_id: str,
    job_id: str,
    question: str,
    answer: str,
    created_at: datetime,
) -> list[MessageDTO]:
    return [
        MessageDTO(
            message_id=message_id,
            session_id="session_1",
            role=MessageRole.user,
            content=question,
            attachments=[],
            metadata={"job_id": job_id},
            created_at=created_at,
            updated_at=created_at,
        ),
        MessageDTO(
            message_id=f"{message_id}_assistant",
            session_id="session_1",
            role=MessageRole.assistant,
            content=answer,
            attachments=[],
            metadata={},
            created_at=created_at + timedelta(seconds=1),
            updated_at=created_at + timedelta(seconds=1),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_text_end", [False, True])
async def test_checkpoint_assistant_closes_trace_crash_gap(
    tmp_path: Path,
    session_bundle_factory,
    with_text_end: bool,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    created = _event(1, include_inline_media=False)
    trace_events: list[Event] = [created]
    if with_text_end:
        trace_events.append(
            TextEndEvent(
                event_id="event_1_text_end",
                job_id="job_1",
                part_id="part_1",
                timestamp=created.timestamp + timedelta(milliseconds=500),
                payload=TextEndPayload(kind="markdown", text="checkpoint 最终回答"),
            )
        )
    store = TurnHistoryStore(sessions_dir)
    service = _build_service(
        sessions_dir,
        store=store,
        trace=_TraceEvents(trace_events),
        legacy_source=_LegacyMessages(
            _checkpoint_pair(
                message_id="message_1",
                job_id="job_1",
                question="Trace 已创建但尚未终态",
                answer="checkpoint 最终回答",
                created_at=created.timestamp,
            )
        ),
    )

    _, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    await service.complete_migration("session_1")

    details, needs_completion = await service.get_details("session_1", ["job_1"])
    assert needs_completion is False
    assert details.items[0].status == JobStatus.completed
    assert details.items[0].final_response == "checkpoint 最终回答"


@pytest.mark.asyncio
async def test_checkpoint_assistant_does_not_duplicate_existing_trace_terminal(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    created = _event(1, include_inline_media=False)
    store = TurnHistoryStore(sessions_dir)
    service = _build_service(
        sessions_dir,
        store=store,
        trace=_TraceEvents([created, _completed(1)]),
        legacy_source=_LegacyMessages(
            _checkpoint_pair(
                message_id="message_1",
                job_id="job_1",
                question="已经完整落入 Trace",
                answer="checkpoint 重复回答",
                created_at=created.timestamp,
            )
        ),
    )

    _, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    await service.complete_migration("session_1")

    details, _ = await service.get_details("session_1", ["job_1"])
    assert details.items[0].status == JobStatus.completed
    assert details.items[0].final_response == "result_1"
    assert details.items[0].revision == 2


@pytest.mark.asyncio
async def test_checkpoint_assistant_follows_merged_job_to_execution_turn(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    first = _event(1, include_inline_media=False)
    merged_source = _event(2, include_inline_media=False)
    merged = JobMergedEvent(
        event_id="event_merge",
        job_id="job_1",
        timestamp=merged_source.timestamp + timedelta(seconds=1),
        payload=JobMergedPayload(
            session_id="session_1",
            merged_job_ids=["job_2"],
            source_message_ids=["message_1", "message_2"],
        ),
    )
    store = TurnHistoryStore(sessions_dir)
    service = _build_service(
        sessions_dir,
        store=store,
        trace=_TraceEvents([first, merged_source, merged]),
        legacy_source=_LegacyMessages(
            _checkpoint_pair(
                message_id="message_2",
                job_id="job_2",
                question="被合并的 steering",
                answer="合并执行后的回答",
                created_at=merged_source.timestamp,
            )
        ),
    )

    _, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    await service.complete_migration("session_1")

    page, _ = await service.list_turns("session_1", limit=20, cursor=None)
    assert [turn.turn_id for turn in page.items] == ["job_1"]
    details, _ = await service.get_details("session_1", ["job_1"])
    assert details.items[0].status == JobStatus.completed
    assert details.items[0].final_response == "合并执行后的回答"


@pytest.mark.asyncio
async def test_first_bootstrap_migrates_checkpoint_history_before_existing_new_trace(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    old_time = datetime(2025, 12, 31, tzinfo=UTC)
    new_created = _event(2, include_inline_media=False)
    store = TurnHistoryStore(sessions_dir)
    service = _build_service(
        sessions_dir,
        store=store,
        trace=_TraceEvents([new_created, _completed(2)]),
        legacy_source=_LegacyMessages(
            _checkpoint_pair(
                message_id="old_message",
                job_id="old_job",
                question="升级前的旧问题",
                answer="升级前的旧回答",
                created_at=old_time,
            )
        ),
    )

    initial, needs_completion = await service.bootstrap("session_1")
    assert needs_completion is True
    assert initial.projection_state == "partial"
    await service.complete_migration("session_1")

    page, needs_completion = await service.list_turns(
        "session_1",
        limit=20,
        cursor=None,
    )
    assert needs_completion is False
    assert [turn.turn_id for turn in page.items] == ["job_2", "old_job"]
