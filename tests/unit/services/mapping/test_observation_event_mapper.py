from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.event import (
    LLMRequestEvent,
    LLMRequestPayload,
    MessageCreatedEvent,
    MessageCreatedPayload,
    StatusChangeEvent,
    StatusChangePayload,
)
from app.services.mapping.observation_event_mapper import map_event_to_observation_sse


def test_child_message_observation_preserves_owner_and_message_time():
    created_at = datetime(2026, 10, 14, 11, 59, tzinfo=UTC)
    published_at = datetime(2026, 10, 14, 12, tzinfo=UTC)
    event = MessageCreatedEvent(
        event_id="evt_child_message",
        job_id="job_child_message",
        timestamp=published_at,
        payload=MessageCreatedPayload(
            message_id="msg_child",
            session_id="ses_child_owner",
            thread_id="thr_child",
            role="user",
            content="child input",
            created_at=created_at,
        ),
    )

    mapped = map_event_to_observation_sse(event)

    assert mapped.event.type == "message.updated"
    assert mapped.event.payload.thread_id == "thr_child"
    assert mapped.event.payload.created_at == created_at
    assert mapped.event.time == published_at
    assert mapped.event.time != mapped.event.payload.created_at


def test_status_change_maps_payload_to_typed_transport_dto():
    event = StatusChangeEvent(
        event_id="evt_status",
        job_id="job_status",
        timestamp=datetime.now(UTC),
        payload=StatusChangePayload(
            status="running",
            reason="agent running",
            session_id="ses_status",
        ),
    )

    mapped = map_event_to_observation_sse(event)

    assert mapped.event.type == "session.status.changed"
    assert mapped.event.payload.session_id == "ses_status"
    assert mapped.event.payload.status == "busy"


def test_non_observation_event_is_exposed_without_fake_status_mapping():
    event = LLMRequestEvent(
        event_id="evt_llm",
        job_id="job_llm",
        timestamp=datetime.now(UTC),
        payload=LLMRequestPayload(model="test-model", timestamp=123),
    )

    mapped = map_event_to_observation_sse(event)

    assert mapped.event.type == "trace.observed"
    assert mapped.event.payload.raw_type == "llm_request"
    assert mapped.raw_type == "llm_request"
