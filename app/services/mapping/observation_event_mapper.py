from __future__ import annotations

from typing import Any

from app.protocol.codecs.session_sse import session_sse_to_proto
from app.protocol.generated.boxteam.workspace.v2 import session_stream_pb2
from app.schemas.event import Event
from app.schemas.internal_v2.session_interaction import (
    JobProgressDTO,
    JobStatusChangedExecutionEventDTO,
    JobStepProgressDTO,
    JobStepUpdatedExecutionEventDTO,
    JobUpdatedExecutionEventDTO,
    MessageObservationDTO,
    MessageUpdatedExecutionEventDTO,
    SessionCompletedExecutionEventDTO,
    SessionErrorExecutionEventDTO,
    SessionErrorPayloadDTO,
    SessionExecutionSseDTO,
    SessionStatusChangedExecutionEventDTO,
    TraceObservedExecutionEventDTO,
    TraceObservedPayloadDTO,
)
from app.schemas.internal_v2.session_status import (
    SessionObservationStateDTO,
    SessionStatusDTO,
)


def _raw_payload(event: Event) -> dict[str, object]:
    return event.model_dump(mode="json")


def _payload_field(event: Event, key: str, default: Any = "") -> Any:
    payload = _raw_payload(event).get("payload")
    if isinstance(payload, dict):
        return payload.get(key, default)
    return default


def _payload_dict(event: Event) -> dict[str, object]:
    payload = _raw_payload(event).get("payload")
    if isinstance(payload, dict):
        return payload
    return {}


def _session_id(event: Event) -> str:
    session_id = _payload_field(event, "session_id", "")
    if isinstance(session_id, str) and session_id:
        return session_id
    return ""


def map_event_to_observation_sse(event: Event) -> SessionExecutionSseDTO:
    payload: dict[str, Any] = _raw_payload(event)
    event_type = event.type

    if event_type == "message_created":
        mapped_event = MessageUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="message.updated",
            time=event.timestamp,
            payload=MessageObservationDTO(**_payload_dict(event)),
        )
    elif event_type == "job_created":
        mapped_event = JobUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="job.updated",
            time=event.timestamp,
            payload=JobProgressDTO(
                job_id=event.job_id, status="accepted", message="job created"
            ),
        )
    elif event_type == "job_started":
        mapped_event = JobStatusChangedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="job.status.changed",
            time=event.timestamp,
            payload=JobProgressDTO(
                job_id=event.job_id, status="running", message="job started"
            ),
        )
    elif event_type == "job_completed":
        mapped_event = SessionCompletedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="session.completed",
            time=event.timestamp,
            payload=JobProgressDTO(
                job_id=event.job_id,
                status="completed",
                message=_payload_field(event, "result", ""),
            ),
        )
    elif event_type == "job_cancelled":
        mapped_event = SessionStatusChangedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="session.status.changed",
            time=event.timestamp,
            payload=SessionStatusDTO(
                session_id=_session_id(event),
                status="idle",
                message="job cancelled",
                active_job_id=None,
            ),
        )
    elif event_type == "job_failed":
        mapped_event = SessionErrorExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="session.error",
            time=event.timestamp,
            payload=SessionErrorPayloadDTO(
                error=_payload_field(event, "error", "job failed")
            ),
        )
    elif event_type == "status_change":
        mapped_event = SessionStatusChangedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="session.status.changed",
            time=event.timestamp,
            payload=SessionStatusDTO(
                session_id=_session_id(event),
                status="busy"
                if _payload_field(event, "status", "") != "idle"
                else "idle",
                message=_payload_field(event, "reason", None),
                active_job_id=_payload_field(event, "blocked_by_job_id", None),
            ),
        )
    elif event_type == "agent_start":
        mapped_event = JobStepUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="job.step.updated",
            time=event.timestamp,
            payload=JobStepProgressDTO(
                agent_id=_payload_field(event, "agent_id", None),
                message=_payload_field(event, "message", None),
            ),
        )
    elif event_type == "agent_step":
        mapped_event = JobStepUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="job.step.updated",
            time=event.timestamp,
            payload=JobStepProgressDTO(
                phase=_payload_field(event, "phase", None),
            ),
        )
    elif event_type == "agent_end":
        mapped_event = SessionStatusChangedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="session.status.changed",
            time=event.timestamp,
            payload=SessionObservationStateDTO(
                session_id=_session_id(event),
                active_job_id=None,
                is_streaming=False,
                is_idle=True,
            ),
        )
    elif event_type == "error":
        mapped_event = SessionErrorExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="session.error",
            time=event.timestamp,
            payload=SessionErrorPayloadDTO(
                error=_payload_field(event, "error", "unknown error")
            ),
        )
    else:
        mapped_event = TraceObservedExecutionEventDTO(
            event_id=event.event_id,
            session_id=_session_id(event),
            job_id=event.job_id,
            type="trace.observed",
            time=event.timestamp,
            payload=TraceObservedPayloadDTO(raw_type=event_type),
        )

    return SessionExecutionSseDTO(
        event=mapped_event, raw_type=event.type, raw_payload=payload
    )


def map_event_to_observation_proto(
    event: Event,
) -> session_stream_pb2.SessionExecutionSse:
    """把内部事件经现有 DTO 映射后转换为公开 Workspace 协议。"""

    return session_sse_to_proto(map_event_to_observation_sse(event))
