from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.event import (
    LLMRequestEvent,
    LLMRequestPayload,
    StatusChangeEvent,
    StatusChangePayload,
)
from app.services.mapping.observation_event_mapper import map_event_to_observation_sse


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
