from __future__ import annotations

from collections.abc import Mapping
from typing import TypeVar, cast

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

_TextDefault = TypeVar("_TextDefault")


def _raw_payload(event: Event) -> dict[str, object]:
    return event.model_dump(mode="json")


def _payload_text(
    event_payload: Mapping[str, object],
    key: str,
    default: _TextDefault,
) -> str | _TextDefault:
    return cast(str | _TextDefault, event_payload.get(key, default))


def _session_id(event_payload: Mapping[str, object]) -> str:
    session_id = event_payload.get("session_id", "")
    if isinstance(session_id, str) and session_id:
        return session_id
    return ""


def map_event_to_observation_sse(event: Event) -> SessionExecutionSseDTO:
    raw_payload = _raw_payload(event)
    raw_event_payload = raw_payload.get("payload")
    event_payload = raw_event_payload if isinstance(raw_event_payload, dict) else {}
    session_id = _session_id(event_payload)
    event_type = event.type

    if event_type == "message_created":
        mapped_event = MessageUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="message.updated",
            time=event.timestamp,
            payload=MessageObservationDTO(**event_payload),
        )
    elif event_type == "job_created":
        mapped_event = JobUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
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
            session_id=session_id,
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
            session_id=session_id,
            job_id=event.job_id,
            type="session.completed",
            time=event.timestamp,
            payload=JobProgressDTO(
                job_id=event.job_id,
                status="completed",
                message=_payload_text(event_payload, "result", ""),
            ),
        )
    elif event_type == "job_cancelled":
        mapped_event = SessionStatusChangedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="session.status.changed",
            time=event.timestamp,
            payload=SessionStatusDTO(
                session_id=session_id,
                status="idle",
                message="job cancelled",
                active_job_id=None,
            ),
        )
    elif event_type == "job_failed":
        mapped_event = SessionErrorExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="session.error",
            time=event.timestamp,
            payload=SessionErrorPayloadDTO(
                error=_payload_text(event_payload, "error", "job failed")
            ),
        )
    elif event_type == "status_change":
        mapped_event = SessionStatusChangedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="session.status.changed",
            time=event.timestamp,
            payload=SessionStatusDTO(
                session_id=session_id,
                status="busy"
                if _payload_text(event_payload, "status", "") != "idle"
                else "idle",
                message=_payload_text(event_payload, "reason", None),
                active_job_id=_payload_text(event_payload, "blocked_by_job_id", None),
            ),
        )
    elif event_type == "agent_start":
        mapped_event = JobStepUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="job.step.updated",
            time=event.timestamp,
            payload=JobStepProgressDTO(
                agent_id=_payload_text(event_payload, "agent_id", None),
                message=_payload_text(event_payload, "message", None),
            ),
        )
    elif event_type == "agent_step":
        mapped_event = JobStepUpdatedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="job.step.updated",
            time=event.timestamp,
            payload=JobStepProgressDTO(
                phase=_payload_text(event_payload, "phase", None),
            ),
        )
    elif event_type == "agent_end":
        mapped_event = SessionStatusChangedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="session.status.changed",
            time=event.timestamp,
            payload=SessionObservationStateDTO(
                session_id=session_id,
                active_job_id=None,
                is_streaming=False,
                is_idle=True,
            ),
        )
    elif event_type == "error":
        mapped_event = SessionErrorExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="session.error",
            time=event.timestamp,
            payload=SessionErrorPayloadDTO(
                error=_payload_text(event_payload, "error", "unknown error")
            ),
        )
    else:
        mapped_event = TraceObservedExecutionEventDTO(
            event_id=event.event_id,
            session_id=session_id,
            job_id=event.job_id,
            type="trace.observed",
            time=event.timestamp,
            payload=TraceObservedPayloadDTO(raw_type=event_type),
        )

    return SessionExecutionSseDTO(
        event=mapped_event, raw_type=event.type, raw_payload=raw_payload
    )


def map_event_to_observation_proto(
    event: Event,
) -> session_stream_pb2.SessionExecutionSse:
    """把内部事件经现有 DTO 映射后转换为公开 Workspace 协议。"""

    return session_sse_to_proto(map_event_to_observation_sse(event))
