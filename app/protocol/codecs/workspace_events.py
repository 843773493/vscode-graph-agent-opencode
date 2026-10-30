from __future__ import annotations

from collections.abc import Mapping

from google.protobuf import json_format
from google.protobuf.descriptor import FieldDescriptor

from app.core.identifier import validate_session_id, validate_thread_id
from app.protocol.codecs.json import struct_from_mapping, struct_to_mapping
from app.protocol.generated.boxteam.workspace.v2 import file_events_pb2, trace_pb2
from app.schemas.internal_v2.workspace import WorkspaceFileChangeBatchDTO

# 字符串的必填、presence 与数组映射直接取自 descriptor，WKT 保留具名转换。
_TRACE_STRING_FIELDS = tuple(
    field for field in trace_pb2.TraceEvent.DESCRIPTOR.fields if field.type == FieldDescriptor.TYPE_STRING
)


def file_change_batch_to_proto(
    value: Mapping[str, object],
) -> file_events_pb2.WorkspaceFileChangeBatch:
    # 结构映射交给 descriptor；URI、目录键和业务取值仍由原 DTO 核定。
    batch = WorkspaceFileChangeBatchDTO.model_validate(value, strict=True)
    result = file_events_pb2.WorkspaceFileChangeBatch()
    json_format.ParseDict(batch.model_dump(mode="json"), result, ignore_unknown_fields=False)
    return result


def file_change_batch_to_json(
    value: file_events_pb2.WorkspaceFileChangeBatch,
) -> dict[str, object]:
    payload = json_format.MessageToDict(
        value, preserving_proto_field_name=True, always_print_fields_with_no_presence=True,
    )
    # protobuf JSON 的 int64 为字符串；原 DTO 还原公开整数、空值与业务默认值。
    return WorkspaceFileChangeBatchDTO.model_validate(payload).model_dump(mode="json")


def sse_error_to_proto(message: str) -> file_events_pb2.SseError:
    if not message:
        raise ValueError("SSE 错误消息不能为空")
    return file_events_pb2.SseError(message=message)


def sse_error_to_json(value: file_events_pb2.SseError) -> dict[str, object]:
    return {"message": value.message}


def trace_to_proto(value: Mapping[str, object]) -> trace_pb2.TraceEvent:
    result = trace_pb2.TraceEvent()
    for field in _TRACE_STRING_FIELDS:
        field_value = value.get(field.name, [] if field.is_repeated else None)
        if field.is_repeated:
            if not isinstance(field_value, list) or not all(isinstance(item, str) for item in field_value):
                raise ValueError(f"Trace 事件 {field.name} 必须是字符串数组")
            getattr(result, field.name).extend(field_value)
        elif field.has_presence:
            if field_value is not None:
                if not isinstance(field_value, str):
                    raise ValueError(f"Trace 事件字段类型错误: {field.name}")
                setattr(result, field.name, field_value)
        else:
            if not isinstance(field_value, str):
                raise TypeError(f"Trace 事件缺少字段: {field.name}")
            setattr(result, field.name, field_value)
    # 2.1：wire 上的 session_id 必须过唯一 canonical 验证器（encode 边界）。
    validate_session_id(result.session_id)
    validate_thread_id(result.thread_id)
    raw = value.get("raw", {})
    if not isinstance(raw, Mapping):
        raise TypeError("Trace 事件 raw 必须是对象")
    result.raw.CopyFrom(struct_from_mapping(raw))
    timestamp = value.get("timestamp")
    if not isinstance(timestamp, str):
        raise TypeError("Trace 事件 timestamp 必须是 ISO 字符串")
    result.timestamp.FromJsonString(timestamp)
    return result


def trace_to_json(value: object) -> dict[str, object]:
    if not isinstance(value, trace_pb2.TraceEvent):
        raise TypeError(f"Trace Protobuf 类型错误: {type(value).__name__}")
    result: dict[str, object] = {}
    for field in _TRACE_STRING_FIELDS:
        if field.is_repeated:
            result[field.name] = list(getattr(value, field.name))
        elif field.has_presence and not value.HasField(field.name):
            result[field.name] = None
        else:
            result[field.name] = getattr(value, field.name)
    return {
        **result,
        "timestamp": value.timestamp.ToJsonString(),
        "raw": struct_to_mapping(value.raw) if value.HasField("raw") else {},
    }
