from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.schemas.event import (
    ErrorEvent,
    ErrorPayload,
    JobCompletedEvent,
    JobCompletedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    JobMergedEvent,
    JobMergedPayload,
    MessageCreatedEvent,
    MessageCreatedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    TextEndEvent,
    TextEndPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from app.services.business.session_turn_history import TurnHistoryProjector
from app.services.infrastructure.turn_history import TurnHistoryStore


@pytest.fixture
def projector(
    tmp_path: Path,
    session_bundle_factory,
) -> tuple[TurnHistoryProjector, TurnHistoryStore, Path]:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir)
    return TurnHistoryProjector(store), store, sessions_dir


def _job_created(index: int) -> JobCreatedEvent:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index)
    return JobCreatedEvent(
        event_id=f"event_job_{index}",
        job_id=f"job_{index}",
        timestamp=timestamp,
        payload=JobCreatedPayload(
            session_id="session_1",
            message=f"问题 {index}",
            agent_id="default",
            message_id=f"message_{index}",
            attachments=[
                {
                    "file_id": f"file_{index}",
                    "name": "图片.png",
                    "content_type": "image/png",
                    "data_url": "data:image/png;base64," + "A" * 10_000,
                }
            ],
            message_created_at=timestamp,
            message_metadata={
                "internal": False,
                "inline_data_url": "data:image/png;base64," + "B" * 10_000,
                "unbounded": "x" * 10_000,
            },
        ),
    )


def test_projector_builds_complete_turn_and_strips_inline_media(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, store, _ = projector
    created = _job_created(1)
    service.apply_event("session_1", created)
    service.apply_event(
        "session_1",
        TextEndEvent(
            event_id="event_text_1",
            part_id="part_1",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=1),
            payload=TextEndPayload(kind="markdown", text="临时回答"),
        ),
    )
    completed = service.apply_event(
        "session_1",
        JobCompletedEvent(
            event_id="event_completed_1",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=2),
            payload=JobCompletedPayload(result="最终回答"),
        ),
    )

    assert completed is not None
    assert completed.revision == 3
    assert completed.final_response == "最终回答"
    assert completed.source_message_ids == ["message_1"]
    serialized = store.get_details("session_1", ["job_1"]).model_dump_json()
    assert "data:image" not in serialized
    assert "inline_data_url" not in serialized
    assert "unbounded" not in serialized


def test_message_created_replaces_job_created_message_without_duplicate(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, store, _ = projector
    created = _job_created(1)
    service.apply_event("session_1", created)
    service.apply_event(
        "session_1",
        MessageCreatedEvent(
            event_id="event_message_1",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=1),
            payload=MessageCreatedPayload(
                message_id="message_1",
                session_id="session_1",
                role="user",
                content="展示投影后的问题",
                created_at=created.timestamp,
            ),
        ),
    )

    detail = store.get_details("session_1", ["job_1"]).items[0]
    assert detail.source_message_ids == ["message_1"]
    assert [message.content for message in detail.user_messages] == ["展示投影后的问题"]


def test_real_message_replaces_legacy_synthetic_message(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, store, _ = projector
    created = _job_created(1)
    legacy_created = created.model_copy(
        update={"payload": created.payload.model_copy(update={"message_id": None})}
    )
    service.apply_event("session_1", legacy_created)
    service.apply_event(
        "session_1",
        MessageCreatedEvent(
            event_id="event_real_message",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=1),
            payload=MessageCreatedPayload(
                message_id="message_real",
                session_id="session_1",
                role="user",
                content="真实消息",
                attachments=[
                    {
                        "file_id": "file_real",
                        "name": "图片.png",
                        "content_type": "image/png",
                        "data_url": "data:image/png;base64,AAAA",
                    }
                ],
                created_at=created.timestamp,
            ),
        ),
    )

    detail = store.get_details("session_1", ["job_1"]).items[0]
    assert detail.source_message_ids == ["message_real"]
    assert [message.message_id for message in detail.user_messages] == ["message_real"]
    assert len(detail.user_messages[0].attachments) == 1


def test_projector_merges_steering_jobs_into_one_execution_turn(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, store, _ = projector
    for index in range(1, 4):
        service.apply_event("session_1", _job_created(index))

    merged = service.apply_event(
        "session_1",
        JobMergedEvent(
            event_id="event_merge",
            job_id="job_2",
            timestamp=datetime(2026, 1, 1, 0, 4, tzinfo=UTC),
            payload=JobMergedPayload(
                session_id="session_1",
                merged_job_ids=["job_3"],
                source_message_ids=["message_2", "message_3"],
            ),
        ),
    )

    assert merged is not None
    assert merged.turn_id == "job_2"
    assert merged.source_message_ids == ["message_2", "message_3"]
    assert merged.merged_job_ids == ["job_3"]
    assert [item.message_id for item in merged.user_messages] == [
        "message_2",
        "message_3",
    ]
    assert [item.turn_id for item in store.list_summaries("session_1").items] == [
        "job_2",
        "job_1",
    ]


def test_projector_restart_replay_is_idempotent(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, store, sessions_dir = projector
    event = _job_created(1)
    assert service.rebuild_from_events("session_1", [event]) == 1

    restarted_store = TurnHistoryStore(sessions_dir)
    restarted = TurnHistoryProjector(restarted_store)
    assert restarted.rebuild_from_events("session_1", [event]) == 1
    detail = restarted_store.get_details("session_1", ["job_1"]).items[0]
    assert detail.revision == 1
    assert store.turn_count("session_1") == 1


def test_non_user_message_only_advances_cursor_without_creating_turn(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, store, _ = projector
    event = MessageCreatedEvent(
        event_id="event_system",
        job_id="job_system",
        timestamp=datetime.now(UTC),
        payload=MessageCreatedPayload(
            message_id="message_system",
            session_id="session_1",
            role="system",
            content="系统消息",
            created_at=datetime.now(UTC),
        ),
    )

    assert service.record_event("session_1", event) is None
    assert store.turn_count("session_1") == 0
    assert store.event_cursor("session_1") is None


def test_projector_rejects_cross_session_event(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, _, _ = projector
    event = _job_created(1).model_copy(
        update={
            "payload": _job_created(1).payload.model_copy(
                update={"session_id": "session_other"}
            )
        }
    )

    with pytest.raises(ValueError, match="跨会话"):
        service.record_event("session_1", event)


@pytest.mark.parametrize("event_kind", ["terminal", "text", "tool"])
def test_orphan_semantic_event_never_creates_phantom_turn(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
    event_kind: str,
) -> None:
    service, store, _ = projector
    now = datetime.now(UTC)
    if event_kind == "terminal":
        event = JobCompletedEvent(
            event_id="event_orphan_terminal",
            job_id="job_orphan",
            timestamp=now,
            payload=JobCompletedPayload(result="done"),
        )
    elif event_kind == "text":
        event = TextEndEvent(
            event_id="event_orphan_text",
            part_id="part_orphan_text",
            job_id="job_orphan",
            timestamp=now,
            payload=TextEndPayload(kind="markdown", text="done"),
        )
    else:
        event = ToolCallStartEvent(
            event_id="event_orphan_tool",
            part_id="part_orphan_tool",
            job_id="job_orphan",
            timestamp=now,
            payload=ToolCallStartPayload(
                execution_id="exec_orphan",
                tool_name="read",
                args={},
            ),
        )

    with pytest.raises(RuntimeError, match="缺少可靠 job_created 起点"):
        service.record_event("session_1", event, source_offset=1)
    assert store.turn_count("session_1") == 0
    assert store.event_cursor("session_1") is None


@pytest.mark.parametrize("terminal_kind", ["interrupt", "agent_error"])
def test_interrupt_and_agent_error_do_not_leave_turn_running(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
    terminal_kind: str,
) -> None:
    service, store, _ = projector
    created = _job_created(1)
    service.record_event("session_1", created, source_offset=1)
    if terminal_kind == "interrupt":
        event = SessionInterruptedEvent(
            event_id="event_terminal",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=1),
            payload=SessionInterruptedPayload(
                session_id="session_1",
                phase="text",
            ),
        )
    else:
        event = ErrorEvent(
            event_id="event_terminal",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=1),
            payload=ErrorPayload(error="boom", phase="agent_execution"),
        )

    result = service.record_event("session_1", event, source_offset=2)

    assert result is not None
    expected = "cancelled" if terminal_kind == "interrupt" else "failed"
    assert result.status.value == expected
    assert result.completed_at == event.timestamp
    assert store.event_cursor("session_1") == "event_terminal"


def test_projection_operation_log_grows_linearly_and_keeps_large_response_once(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    store = TurnHistoryStore(sessions_dir, compaction_threshold=10_000)
    service = TurnHistoryProjector(store)
    created = _job_created(1)
    service.record_event("session_1", created, source_offset=1)
    for index in range(120):
        service.record_event(
            "session_1",
            ToolCallStartEvent(
                event_id=f"event_tool_{index:03d}",
                part_id=f"part_tool_{index:03d}",
                job_id="job_1",
                timestamp=created.timestamp + timedelta(milliseconds=index + 1),
                payload=ToolCallStartPayload(
                    execution_id=f"exec_tool_{index:03d}",
                    tool_name="read",
                    args={"path": f"file-{index:03d}.md", "pad": "x" * 128},
                ),
            ),
            source_offset=index + 2,
        )
    large_response = "UNIQUE-LARGE-RESPONSE-" + "r" * 20_000
    service.record_event(
        "session_1",
        TextEndEvent(
            event_id="event_large_text",
            part_id="part_large_text",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=2),
            payload=TextEndPayload(kind="markdown", text=large_response),
        ),
        source_offset=122,
    )
    service.record_event(
        "session_1",
        JobCompletedEvent(
            event_id="event_large_completed",
            job_id="job_1",
            timestamp=created.timestamp + timedelta(seconds=3),
            payload=JobCompletedPayload(result=large_response),
        ),
        source_offset=123,
    )

    root = store._files.root("session_1")
    operation_path = next(root.glob("operations.*.jsonl"))
    record_path = store._files.turn_record_path("session_1", "job_1")
    operation_bytes = operation_path.read_bytes()
    assert operation_path.stat().st_size < record_path.stat().st_size * 8
    assert operation_bytes.count(large_response.encode("utf-8")) == 1


def test_destructive_rebuild_failure_keeps_old_authoritative_projection(
    projector: tuple[TurnHistoryProjector, TurnHistoryStore, Path],
) -> None:
    service, store, _ = projector
    service.rebuild_from_events("session_1", [_job_created(1)])
    old_epoch = store.projection_epoch("session_1")
    invalid_midstream = TextEndEvent(
        event_id="event_invalid_midstream",
        part_id="part_invalid_midstream",
        job_id="job_missing_start",
        timestamp=datetime.now(UTC),
        payload=TextEndPayload(kind="markdown", text="invalid"),
    )

    with pytest.raises(RuntimeError, match="缺少可靠 job_created 起点"):
        service.rebuild_from_events(
            "session_1",
            [_job_created(2), invalid_midstream],
            destructive=True,
        )

    assert store.projection_epoch("session_1") == old_epoch
    assert store.get_turn("session_1", "job_1") is not None
    assert store.get_turn("session_1", "job_2") is None
