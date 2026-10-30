from __future__ import annotations

from datetime import UTC
from typing import cast

from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor
from google.protobuf.message import Message
from pydantic import BaseModel

from app.core.identifier import validate_session_id
from app.protocol.codecs.json import (
    map_enum_values,
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
    SessionExecutionEventBaseDTO,
    SessionExecutionSseDTO,
    SessionStatusChangedExecutionEventDTO,
)
from app.schemas.internal_v2.session_status import (
    SessionObservationStateDTO,
    SessionStatusDTO,
)

# 事件名与 oneof 字段遵循同一命名合同，不再逐类维护派发表。
_PAYLOAD_FIELDS = {
    field.name.replace("_", "."): field.name
    for field in session_interaction_pb2.SessionExecutionEvent.DESCRIPTOR.oneofs_by_name["payload"].fields
}


def _parse_model(model: BaseModel, target: Message) -> None:
    payload = cast(dict[str, object], model.model_dump(mode="json"))
    json_format.ParseDict(
        map_enum_values(payload, cast(Descriptor, target.DESCRIPTOR), to_proto=True),
        target, ignore_unknown_fields=False,
    )


def _header(event: SessionExecutionEventBaseDTO) -> session_interaction_pb2.SessionExecutionEventHeader:
    # 2.1：wire 上的 session_id 必须过唯一 canonical 验证器（encode 边界）。
    validate_session_id(event.session_id)
    header = session_interaction_pb2.SessionExecutionEventHeader(
        event_id=event.event_id,
        session_id=event.session_id,
        time=timestamp_from_datetime(event.time),
    )
    if event.job_id is not None:
        header.job_id = event.job_id
    return header


def _session_status(
    payload: SessionStatusDTO | SessionObservationStateDTO,
) -> session_interaction_pb2.SessionStatus:
    validate_session_id(payload.session_id)
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
    payload_field = _PAYLOAD_FIELDS.get(source_event.type)
    if payload_field is None:
        raise TypeError(f"未支持的 Workspace SSE 事件 DTO: {type(source_event).__name__}")
    if isinstance(source_event, SessionStatusChangedExecutionEventDTO):
        event.session_status_changed.CopyFrom(_session_status(source_event.payload))
    else:
        _parse_model(source_event.payload, getattr(event, payload_field))

    return session_stream_pb2.SessionExecutionSse(
        event=event,
        raw_type=value.raw_type,
        raw_payload=struct_from_mapping(value.raw_payload),
    )


def _event_payload_to_json(event: session_interaction_pb2.SessionExecutionEvent) -> dict[str, object]:
    payload_case = event.WhichOneof("payload")
    if payload_case is None:
        raise ValueError(f"Workspace SSE 事件缺少 payload: type={event.type}")
    value = getattr(event, payload_case)
    if isinstance(value, job_pb2.JobProgress):
        payload = json_format.MessageToDict(
            value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True,
        )
        return JobProgressDTO.model_validate(
            map_enum_values(payload, cast(Descriptor, value.DESCRIPTOR), to_proto=False),
        ).model_dump(mode="json")
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
        always_print_fields_with_no_presence=True,
    ))
    if payload_case == "message_updated":
        if "metadata" not in payload:
            payload["metadata"] = {}
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
        "time": header.time.ToDatetime(tzinfo=UTC).isoformat().replace("+00:00", "Z"),
        "payload": _event_payload_to_json(event),
    }
    return {
        "event": event_json,
        "raw_type": value.raw_type,
        "raw_payload": struct_to_mapping(value.raw_payload)
        if value.HasField("raw_payload")
        else {},
    }
