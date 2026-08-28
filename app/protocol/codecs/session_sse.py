from __future__ import annotations

from typing import cast

from google.protobuf import json_format
from google.protobuf.message import Message
from pydantic import BaseModel

from app.protocol.codecs.json import (
    struct_from_mapping,
    struct_to_mapping,
    timestamp_from_datetime,
)
from app.protocol.generated.boxteam.workspace.v2 import (
    job_pb2,
    session_interaction_pb2,
    session_stream_pb2,
)
from app.schemas.internal_v2.session_interaction import (
    JobProgressDTO,
    JobStatusChangedExecutionEventDTO,
    JobStepUpdatedExecutionEventDTO,
    JobUpdatedExecutionEventDTO,
    MessageUpdatedExecutionEventDTO,
    SessionCompletedExecutionEventDTO,
    SessionErrorExecutionEventDTO,
    SessionExecutionEventBaseDTO,
    SessionExecutionSseDTO,
    SessionStatusChangedExecutionEventDTO,
    TraceObservedExecutionEventDTO,
)
from app.schemas.internal_v2.session_status import (
    SessionObservationStateDTO,
    SessionStatusDTO,
)


def _parse_model(model: BaseModel, target: Message) -> None:
    payload = cast(dict[str, object], model.model_dump(mode="json"))
    json_format.ParseDict(payload, target, ignore_unknown_fields=False)


def _header(event: SessionExecutionEventBaseDTO) -> session_interaction_pb2.SessionExecutionEventHeader:
    header = session_interaction_pb2.SessionExecutionEventHeader(
        event_id=event.event_id,
        session_id=event.session_id,
        time=timestamp_from_datetime(event.time),
    )
    if event.job_id is not None:
        header.job_id = event.job_id
    return header


def _job_status(value: str) -> int:
    status_name = f"JOB_STATUS_{value.upper()}"
    status = getattr(job_pb2, status_name, None)
    if not isinstance(status, int):
        raise TypeError(f"SSE JobStatus 无法映射到 Protobuf: {value}")
    return status


def _job_progress(payload: JobProgressDTO) -> job_pb2.JobProgress:
    status_value = getattr(payload.status, "value", payload.status)
    progress = job_pb2.JobProgress(
        job_id=payload.job_id,
        status=_job_status(str(status_value)),
        progress=payload.progress,
    )
    if payload.current_step_id is not None:
        progress.current_step_id = payload.current_step_id
    if payload.message is not None:
        progress.message = payload.message
    return progress


def _session_status(
    payload: SessionStatusDTO | SessionObservationStateDTO,
) -> session_interaction_pb2.SessionStatus:
    if isinstance(payload, SessionStatusDTO):
        status = session_interaction_pb2.SessionStatus(
            session_id=payload.session_id,
            status=payload.status,
        )
        if payload.message is not None:
            status.message = payload.message
        if payload.active_job_id is not None:
            status.active_job_id = payload.active_job_id
        if payload.waiting is not None:
            status.waiting.CopyFrom(
                struct_from_mapping(
                    cast(dict[str, object], payload.waiting.model_dump(mode="json"))
                )
            )
        return status
    status = session_interaction_pb2.SessionStatus(
        session_id=payload.session_id,
        observation_state="streaming" if payload.is_streaming else "idle",
        is_streaming=payload.is_streaming,
        is_idle=payload.is_idle,
    )
    if payload.active_job_id is not None:
        status.active_job_id = payload.active_job_id
    return status


def session_sse_to_proto(value: SessionExecutionSseDTO) -> session_stream_pb2.SessionExecutionSse:
    """把现有 SSE DTO 转换为 Workspace Protobuf 事件。"""

    source_event = value.event
    event = session_interaction_pb2.SessionExecutionEvent(
        type=source_event.type,
        header=_header(source_event),
    )
    if isinstance(source_event, MessageUpdatedExecutionEventDTO):
        _parse_model(source_event.payload, event.message_updated)
    elif isinstance(source_event, JobUpdatedExecutionEventDTO):
        event.job_updated.CopyFrom(_job_progress(source_event.payload))
    elif isinstance(source_event, JobStepUpdatedExecutionEventDTO):
        _parse_model(source_event.payload, event.job_step_updated)
    elif isinstance(source_event, JobStatusChangedExecutionEventDTO):
        event.job_status_changed.CopyFrom(_job_progress(source_event.payload))
    elif isinstance(source_event, SessionStatusChangedExecutionEventDTO):
        event.session_status_changed.CopyFrom(_session_status(source_event.payload))
    elif isinstance(source_event, SessionCompletedExecutionEventDTO):
        event.session_completed.CopyFrom(_job_progress(source_event.payload))
    elif isinstance(source_event, SessionErrorExecutionEventDTO):
        _parse_model(source_event.payload, event.session_error)
    elif isinstance(source_event, TraceObservedExecutionEventDTO):
        _parse_model(source_event.payload, event.trace_observed)
    else:
        raise TypeError(f"未支持的 Workspace SSE 事件 DTO: {type(source_event).__name__}")

    return session_stream_pb2.SessionExecutionSse(
        event=event,
        raw_type=value.raw_type,
        raw_payload=struct_from_mapping(value.raw_payload),
    )


def _job_progress_to_json(value: job_pb2.JobProgress) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": value.job_id,
        "status": job_pb2.JobStatus.Name(value.status).removeprefix("JOB_STATUS_").lower(),
        "current_step_id": value.current_step_id if value.HasField("current_step_id") else None,
        "progress": value.progress,
        "message": value.message if value.HasField("message") else None,
    }
    return payload


def _event_payload_to_json(event: session_interaction_pb2.SessionExecutionEvent) -> dict[str, object]:
    payload_case = event.WhichOneof("payload")
    if payload_case is None:
        raise ValueError(f"Workspace SSE 事件缺少 payload: type={event.type}")
    if payload_case in {"job_updated", "job_status_changed", "session_completed"}:
        return _job_progress_to_json(getattr(event, payload_case))
    if payload_case == "session_status_changed":
        value = event.session_status_changed
        if value.observation_state:
            payload: dict[str, object] = {
                "session_id": value.session_id,
                "active_job_id": value.active_job_id if value.HasField("active_job_id") else None,
                "is_streaming": value.is_streaming,
                "is_idle": value.is_idle,
            }
        else:
            payload = {
                "session_id": value.session_id,
                "status": value.status,
                "message": value.message if value.HasField("message") else None,
                "active_job_id": value.active_job_id if value.HasField("active_job_id") else None,
                "waiting": struct_to_mapping(value.waiting) if value.HasField("waiting") else None,
            }
        return payload
    payload_message = cast(Message, getattr(event, payload_case))
    payload = cast(dict[str, object], json_format.MessageToDict(
        payload_message,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=False,
    ))
    if payload_case == "message_updated":
        payload.setdefault("attachments", [])
        payload.setdefault("metadata", {})
    elif payload_case == "job_step_updated":
        payload.setdefault("agent_id", None)
        payload.setdefault("message", None)
        payload.setdefault("phase", None)
    return payload


def session_sse_to_json(value: session_stream_pb2.SessionExecutionSse) -> dict[str, object]:
    """把 Protobuf 事件还原为现有 SSE 的 JSON 外形。"""

    if not value.HasField("event"):
        raise ValueError("Workspace SSE Protobuf 消息缺少 event")
    event = value.event
    if not event.HasField("header"):
        raise ValueError(f"Workspace SSE 事件缺少 header: type={event.type}")
    header = event.header
    event_json: dict[str, object] = {
        "event_id": header.event_id,
        "session_id": header.session_id,
        "job_id": header.job_id if header.HasField("job_id") else None,
        "type": event.type,
        "time": header.time.ToDatetime().isoformat().replace("+00:00", "Z"),
        "payload": _event_payload_to_json(event),
    }
    return {
        "event": event_json,
        "raw_type": value.raw_type,
        "raw_payload": struct_to_mapping(value.raw_payload)
        if value.HasField("raw_payload")
        else {},
    }
