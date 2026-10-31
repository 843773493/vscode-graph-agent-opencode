from __future__ import annotations

import copy
from collections.abc import Mapping
from functools import cache
from typing import Any, cast

from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message

from app.core.identifier import validate_session_id, validate_thread_id
from app.protocol.codecs.json import (
    event_payload_fields,
    map_enum_values,
    message_to_json,
    timestamp_from_datetime,
)
from app.protocol.generated.boxteam.workspace.message.v1 import message_stream_pb2

_PAYLOAD_FIELDS = event_payload_fields(
    cast(Descriptor, message_stream_pb2.MessageStreamEvent.DESCRIPTOR), aliases={
        "tool_call": ("tool_call",),
        "tool_call_delta": ("tool_call.delta",),
        "tool_call_completed": ("tool_call.completed",),
        "activity": ("activity.started", "activity.updated", "activity.completed", "activity.failed"),
    },
)


@cache
def _required_envelope_fields(descriptor: Descriptor) -> tuple[FieldDescriptor, ...]:
    # 非 presence 标量是既有必填信封；关联身份、时间与 payload 沿各自合同转换。
    return tuple(field for field in descriptor.fields if not field.has_presence)


@cache
def _identity_fields(descriptor: Descriptor) -> tuple[str, ...]:
    # 可选字符串是信封关联身份；job_id 沿既有合同独立传输，不从 payload 补齐或核冲突。
    return tuple(field.name for field in descriptor.fields
                 if field.type == FieldDescriptor.TYPE_STRING and field.has_presence
                 and field.name != "job_id" and field not in descriptor.oneofs_by_name["payload"].fields)


def message_stream_to_proto(
    value: Mapping[str, Any],
) -> message_stream_pb2.MessageStreamEvent:
    """把内部事件字典转换为严格的 v1 Protobuf 消息。"""
    # 2.1：wire 上的 session_id 必须过唯一 canonical 验证器（encode 边界）。
    validate_session_id(_required_string(value, "session_id"))
    validate_thread_id(_required_string(value, "thread_id"))
    event = message_stream_pb2.MessageStreamEvent()
    readers = {FieldDescriptor.TYPE_STRING: _required_string, FieldDescriptor.TYPE_INT64: _required_int}
    for field in _required_envelope_fields(cast(Descriptor, event.DESCRIPTOR)):
        setattr(event, field.name, readers[field.type](value, field.name))
    payload_field = _PAYLOAD_FIELDS.get(event.type)
    if payload_field is None:
        raise ValueError(f"消息流事件类型未注册: {event.type}")
    emitted_at = value.get("emitted_at")
    if emitted_at is not None:
        if not isinstance(emitted_at, str):
            raise TypeError("消息流 emitted_at 必须是 ISO 字符串")
        from datetime import datetime

        parsed = datetime.fromisoformat(emitted_at)
        event.emitted_at.CopyFrom(timestamp_from_datetime(parsed))
    identity_fields = _identity_fields(cast(Descriptor, event.DESCRIPTOR))
    for field_name in (*identity_fields, "job_id"):
        field_value = value.get(field_name)
        if field_value is not None:
            if not isinstance(field_value, str) or not field_value:
                raise TypeError(f"消息流 {field_name} 必须是非空字符串")
            setattr(event, field_name, field_value)
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError(f"消息流 payload 必须是对象: type={event.type}")
    for field_name in identity_fields:
        envelope_value = value.get(field_name)
        payload_value = payload.get(field_name)
        for field_value, source in (
            (envelope_value, "信封"),
            (payload_value, "payload"),
        ):
            if field_value is not None and (
                not isinstance(field_value, str) or not field_value
            ):
                raise TypeError(
                    f"消息流 {field_name} 必须是非空字符串: source={source}"
                )
        if (
            envelope_value is not None
            and payload_value is not None
            and envelope_value != payload_value
        ):
            raise ValueError(
                f"消息流 {field_name} 信封与 payload 身份不一致"
            )
        if envelope_value is None and isinstance(payload_value, str):
            setattr(event, field_name, payload_value)
    target = getattr(event, payload_field.name)
    if event.type == "stream.snapshot" and isinstance(payload.get("snapshot"), Mapping):
        payload = cast(Mapping[str, Any], payload["snapshot"])
    json_format.ParseDict(
        map_enum_values(_normalize_payload(event.type, payload), target.DESCRIPTOR, to_proto=True),
        target,
        ignore_unknown_fields=False,
    )
    return event


def message_stream_to_json(
    value: message_stream_pb2.MessageStreamEvent,
) -> dict[str, Any]:
    """把 v1 Protobuf 消息还原为 SSE/HTTP 使用的 snake_case JSON。"""
    payload_field = value.WhichOneof("payload")
    if payload_field is None:
        raise ValueError(f"消息流事件缺少 payload: type={value.type}")
    expected = _PAYLOAD_FIELDS.get(value.type)
    if expected is None or payload_field != expected.name:
        raise ValueError(f"消息流事件 type 与 payload 不一致: expected={expected.name if expected else None}, actual={payload_field}")
    payload_message = cast(Message, getattr(value, payload_field))
    payload = map_enum_values(message_to_json(payload_message), cast(Descriptor, payload_message.DESCRIPTOR), to_proto=False)
    if value.type == "stream.snapshot":
        _restore_snapshot_integers(payload, payload_message)
        payload = _denormalize_snapshot(payload)
    result: dict[str, Any] = {field.name: getattr(value, field.name)
                              for field in _required_envelope_fields(cast(Descriptor, value.DESCRIPTOR))}
    result["payload"] = payload
    if value.HasField("emitted_at"):
        result["emitted_at"] = value.emitted_at.ToDatetime().isoformat().replace(
            "+00:00", "Z"
        )
    for field_name in (*_identity_fields(cast(Descriptor, value.DESCRIPTOR)), "job_id"):
        if value.HasField(field_name):
            result[field_name] = getattr(value, field_name)
    return result


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"消息流 {key} 必须是非空字符串")
    return result


def _required_int(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise TypeError(f"消息流 {key} 必须是整数")
    return result


def _normalize_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = (
        copy.deepcopy(dict(payload))
        if event_type == "stream.snapshot"
        else dict(payload)
    )
    # TODO: 公共 message.v1 schema 增加这些字段后再移除该边界投影。
    # status 属于内部 tool-call 聚合状态，ToolCallDelta 只承载参数增量。
    if event_type == "tool_call.delta":
        normalized.pop("status", None)
    if event_type == "tool_call.completed":
        # ToolCallCompleted 是参数生命周期事件，公共 message.v1 没有 error
        # 字段；失败原因由对应的 ToolCompleted/StreamFailure 承载。
        normalized.pop("error", None)
    if event_type == "tool.completed":
        _normalize_tool_status(normalized)
    if event_type.startswith("activity."):
        # Activity 的完成原因属于内部诊断，公共 proto 未声明。
        normalized.pop("completion_reason", None)
    if event_type == "stream.snapshot":
        snapshot = normalized.get("snapshot", normalized)
        if isinstance(snapshot, dict):
            for envelope_field in ("session_id", "thread_id", "turn_id", "turn_stream_id", "job_id"):
                snapshot.pop(envelope_field, None)
            failure = snapshot.get("failure")
            if isinstance(failure, dict):
                # model.failed 事件历史上会把内部模型错误暂存到顶层
                # failure；公共快照这里实际要求的是 StreamFailure，不能
                # 把 model_call_id、attempt 等内部字段直接交给 protobuf。
                public_failure = {field.name: failure[field.name]
                                  for field in message_stream_pb2.StreamFailure.DESCRIPTOR.fields
                                  if field.name in failure}
                if "code" not in public_failure:
                    error_code = failure.get("error_code")
                    public_failure["code"] = (
                        error_code
                        if isinstance(error_code, str) and error_code
                        else "model_error"
                    )
                if "message" not in public_failure:
                    public_failure["message"] = str(
                        failure.get("error_code") or "模型调用失败"
                    )
                snapshot["failure"] = public_failure
            elif failure is not None:
                snapshot["failure"] = {
                    "code": "invalid_snapshot_failure",
                    "message": "消息流快照中的失败详情格式无效",
                }
            for execution in snapshot.get("tool_executions", []):
                if not isinstance(execution, dict):
                    continue
                # tool execution 聚合需要记录模型调用归属，但公共
                # ToolExecutionSnapshot 没有该内部对账字段。
                execution.pop("model_call_id", None)
                _normalize_tool_status(execution)
            # TODO: ModelCallSnapshot 只公开生命周期和结果状态；错误详情由
            # StreamFailure 承载，不能把 ModelFailed 的内部字段泄漏进 snapshot。
            for model_call in snapshot.get("model_calls", []):
                if isinstance(model_call, dict):
                    model_call.pop("error_code", None)
                    model_call.pop("message", None)
                    # reason 是内部完成/重试诊断字段，公共 schema 使用
                    # completion_reason，不能把两者混写后交给严格 protobuf 解析。
                    model_call.pop("reason", None)
            for tool_call in snapshot.get("tool_calls", []):
                if isinstance(tool_call, dict):
                    # tool-call 聚合需要记录模型调用归属，但公共 ToolCall
                    # 没有该内部对账字段；运行中的快照也必须经过同一投影。
                    tool_call.pop("model_call_id", None)
                    # tool-call 聚合会保留旧版本的内部错误详情，但公共
                    # ToolCall 没有 error 字段；终态 StreamFailure 承载对用户
                    # 可见的失败原因，不能让旧字段把整个 snapshot 编码成 500。
                    tool_call.pop("error", None)
            for activity in snapshot.get("activities", []):
                if isinstance(activity, dict):
                    # Activity 的完成原因属于内部诊断字段，公共快照只投影
                    # schema 已声明的生命周期、结果和时间字段。
                    activity.pop("completion_reason", None)
            for block in snapshot.get("blocks", []):
                if isinstance(block, dict):
                    # model_call_id 是内部 checkpoint 的对账字段，公共
                    # MessageBlockSnapshot 只暴露 projection，避免把 attempt
                    # 归属重复塞进 block payload。
                    block.pop("model_call_id", None)
    else:
        # workspace_id 属于消息流信封身份，普通 oneof payload 不重复承载。
        normalized.pop("workspace_id", None)
    if event_type not in {
        "model.started",
        "model.completed",
        "model.retrying",
        "model.failed",
    }:
        # model_call_id 是 block/tool/activity 事件的内部归属身份，公共协议
        # 只在 MessageStreamEvent 信封中承载；把它继续传给 oneof payload 会让
        # 严格 protobuf 在 block.started 等首个增量事件处直接中止 SSE。
        normalized.pop("model_call_id", None)
    return normalized


def _normalize_tool_status(payload: dict[str, Any]) -> None:
    # outcome_unknown 的状态/结果拆分属于业务语义，不能从 enum 名称推导。
    if payload.get("status") == "outcome_unknown":
        payload["status"] = "completed"
        payload.setdefault("outcome", "outcome_unknown")
    # TODO: 仅为已有事件日志提供读取兼容；新写入统一使用 completed。
    elif payload.get("status") == "succeeded":
        payload["status"] = "completed"


def _restore_snapshot_integers(payload: dict[str, object], message: Message) -> None:
    # Protobuf JSON 将64位整数转成字符串；只还原实际声明并出现的字段，不解释动态正文。
    for field, value in message.ListFields():
        if field.cpp_type in (FieldDescriptor.CPPTYPE_INT64, FieldDescriptor.CPPTYPE_UINT64):
            payload[field.name] = list(value) if field.is_repeated else value
        elif field.message_type is not None and not field.message_type.full_name.startswith("google.protobuf."):
            if field.is_repeated:
                items = cast(list[dict[str, object]], payload[field.name])
                for item, source in zip(items, value, strict=True):
                    _restore_snapshot_integers(item, source)
            else:
                _restore_snapshot_integers(cast(dict[str, object], payload[field.name]), value)


def _denormalize_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    interrupt_request_id = payload.pop("interrupt_request_id", None)
    interrupt_status = payload.pop("interrupt_status", None)
    if interrupt_request_id is not None or interrupt_status is not None:
        payload["interrupt_state"] = {
            "request_id": interrupt_request_id,
            "status": interrupt_status,
        }
    return payload
