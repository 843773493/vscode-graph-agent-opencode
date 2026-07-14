from __future__ import annotations

from datetime import datetime, timezone

from app.schemas.event import StatusChangeEvent, StatusChangePayload
from app.services.mapping.observation_event_mapper import map_event_to_observation_sse


def test_status_change_maps_payload_to_transport_dictionary():
    event = StatusChangeEvent(
        event_id="evt_status",
        job_id="job_status",
        timestamp=datetime.now(timezone.utc),
        payload=StatusChangePayload(
            status="running",
            reason="agent running",
            session_id="ses_status",
        ),
    )

    mapped = map_event_to_observation_sse(event)

    assert mapped.event.type == "session.status.changed"
    assert isinstance(mapped.event.payload, dict)
    assert mapped.event.payload["session_id"] == "ses_status"
    assert mapped.event.payload["status"] == "busy"
