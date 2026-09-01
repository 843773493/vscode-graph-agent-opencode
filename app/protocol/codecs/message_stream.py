from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, cast

from google.protobuf import json_format
from google.protobuf.message import Message

from app.protocol.codecs.json import message_to_json, timestamp_from_datetime
from app.protocol.generated.boxteam.workspace.message.v1 import message_stream_pb2

_PAYLOAD_FIELDS: dict[str, str] = {
    "stream.opened": "stream_opened",
    "model.started": "model_started",
    "model.completed": "model_completed",
    "model.retrying": "model_retrying",
    "model.failed": "model_failed",
    "block.started": "block_started",
    "block.delta": "block_delta",
    "block.completed": "block_completed",
    "tool_call": "tool_call",
    "tool_call.delta": "tool_call_delta",
    "tool_call.completed": "tool_call_completed",
    "tool.started": "tool_started",
    "tool.completed": "tool_completed",
    "activity.started": "activity",
    "activity.updated": "activity",
    "activity.completed": "activity",
    "activity.failed": "activity",
    "interrupt.requested": "interrupt_requested",
    "interrupt.rejected": "interrupt_rejected",
    "stream.completed": "stream_completed",
    "stream.interrupted": "stream_interrupted",
    "stream.failed": "stream_failed",
    "stream.snapshot": "stream_snapshot",
}


def message_stream_to_proto(
    value: Mapping[str, Any],
) -> message_stream_pb2.MessageStreamEvent:
    """把内部事件字典转换为严格的 v1 Protobuf 消息。"""
    event = message_stream_pb2.MessageStreamEvent(
        event_id=_required_string(value, "event_id"),
        session_id=_required_string(value, "session_id"),
        turn_id=_required_string(value, "turn_id"),
        turn_stream_id=_required_string(value, "turn_stream_id"),
        event_seq=_required_int(value, "event_seq"),
        type=_required_string(value, "type"),
    )
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
    for field_name in ("model_call_id", "block_id", "tool_execution_id"):
        field_value = value.get(field_name)
        if field_value is not None:
            if not isinstance(field_value, str) or not field_value:
                raise TypeError(f"消息流 {field_name} 必须是非空字符串")
            setattr(event, field_name, field_value)
    job_id = value.get("job_id")
    if job_id is not None:
        if not isinstance(job_id, str) or not job_id:
            raise TypeError("消息流 job_id 必须是非空字符串")
        event.job_id = job_id
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise TypeError(f"消息流 payload 必须是对象: type={event.type}")
    target = getattr(event, payload_field)
    if event.type == "stream.snapshot" and isinstance(payload.get("snapshot"), Mapping):
        payload = cast(Mapping[str, Any], payload["snapshot"])
    json_format.ParseDict(
        _normalize_payload(event.type, payload),
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
    payload_message = cast(Message, getattr(value, payload_field))
    payload = message_to_json(payload_message)
    if value.type == "stream.snapshot":
        payload = _denormalize_snapshot(payload)
    else:
        payload = _denormalize_payload(value.type, payload)
    result: dict[str, Any] = {
        "event_id": value.event_id,
        "session_id": value.session_id,
        "turn_id": value.turn_id,
        "turn_stream_id": value.turn_stream_id,
        "event_seq": value.event_seq,
        "type": value.type,
        "payload": payload,
    }
    if value.HasField("emitted_at"):
        result["emitted_at"] = value.emitted_at.ToDatetime().isoformat().replace(
            "+00:00", "Z"
        )
    for field_name in ("model_call_id", "block_id", "tool_execution_id", "job_id"):
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
    normalized = copy.deepcopy(dict(payload))
    enum_maps: dict[str, dict[str, str]] = {
        "status": {
            "open": "STREAM_STATUS_OPEN",
            "interrupting": "STREAM_STATUS_INTERRUPTING",
            "completed": "STREAM_STATUS_COMPLETED",
            "interrupted": "STREAM_STATUS_INTERRUPTED",
            "failed": "STREAM_STATUS_FAILED",
        },
        "outcome": {
            "accepted": "MODEL_CALL_OUTCOME_ACCEPTED",
            "validation_failed": "MODEL_CALL_OUTCOME_VALIDATION_FAILED",
            "upstream_error": "MODEL_CALL_OUTCOME_UPSTREAM_ERROR",
            "execution_lost": "MODEL_CALL_OUTCOME_EXECUTION_LOST",
            "user_interrupt": "MODEL_CALL_OUTCOME_USER_INTERRUPT",
        },
        "status_tool": {
            "running": "TOOL_EXECUTION_STATUS_RUNNING",
            "completed": "TOOL_EXECUTION_STATUS_COMPLETED",
            # TODO: 仅为已有事件日志提供读取兼容；新写入统一使用 completed。
            "succeeded": "TOOL_EXECUTION_STATUS_COMPLETED",
            "failed": "TOOL_EXECUTION_STATUS_FAILED",
        },
        "outcome_tool": {
            "success": "TOOL_EXECUTION_OUTCOME_SUCCESS",
            "provider_error": "TOOL_EXECUTION_OUTCOME_PROVIDER_ERROR",
            "execution_lost": "TOOL_EXECUTION_OUTCOME_EXECUTION_LOST",
            "outcome_unknown": "TOOL_EXECUTION_OUTCOME_OUTCOME_UNKNOWN",
        },
        "activity_status": {
            "running": "ACTIVITY_STATUS_RUNNING",
            "waiting": "ACTIVITY_STATUS_WAITING",
            "stopping": "ACTIVITY_STATUS_STOPPING",
            "completed": "ACTIVITY_STATUS_COMPLETED",
            "failed": "ACTIVITY_STATUS_FAILED",
            "unknown": "ACTIVITY_STATUS_UNKNOWN",
        },
        "activity_outcome": {
            "success": "ACTIVITY_OUTCOME_SUCCESS",
            "user_interrupt": "ACTIVITY_OUTCOME_USER_INTERRUPT",
            "provider_error": "ACTIVITY_OUTCOME_PROVIDER_ERROR",
            "execution_lost": "ACTIVITY_OUTCOME_EXECUTION_LOST",
            "outcome_unknown": "ACTIVITY_OUTCOME_OUTCOME_UNKNOWN",
        },
        "operation": {
            "append": "BLOCK_DELTA_OPERATION_APPEND",
            "item_upsert": "BLOCK_DELTA_OPERATION_ITEM_UPSERT",
            "item_patch": "BLOCK_DELTA_OPERATION_ITEM_PATCH",
            "redacted": "BLOCK_DELTA_OPERATION_REDACTED",
        },
    }
    if (
        event_type in {"stream.opened", "stream.completed", "stream.interrupted"}
        and isinstance(normalized.get("status"), str)
    ):
        normalized["status"] = enum_maps["status"].get(
            normalized["status"], normalized["status"]
        )
    if (
        event_type in {"model.completed", "model.failed"}
        and isinstance(normalized.get("outcome"), str)
    ):
        normalized["outcome"] = enum_maps["outcome"].get(
            normalized["outcome"], normalized["outcome"]
        )
    if event_type == "block.delta" and isinstance(normalized.get("operation"), str):
        normalized["operation"] = enum_maps["operation"].get(
            normalized["operation"], normalized["operation"]
        )
    # TODO: 公共 message.v1 schema 增加这些字段后再移除该边界投影。
    # status 属于内部 tool-call 聚合状态，ToolCallDelta 只承载参数增量。
    if event_type == "tool_call.delta":
        normalized.pop("status", None)
    if event_type == "tool.completed" and isinstance(normalized.get("status"), str):
        if normalized["status"] == "outcome_unknown":
            normalized["status"] = "completed"
            normalized.setdefault("outcome", "outcome_unknown")
        normalized["status"] = enum_maps["status_tool"].get(
            normalized["status"], normalized["status"]
        )
    if event_type == "tool.completed" and isinstance(normalized.get("outcome"), str):
        normalized["outcome"] = enum_maps["outcome_tool"].get(
            normalized["outcome"], normalized["outcome"]
        )
    if event_type.startswith("activity."):
        # Activity 的完成原因属于内部诊断字段，message.v1 公共 Activity
        # 尚未声明该字段，不能把它交给严格 protobuf 解析。
        normalized.pop("completion_reason", None)
        if isinstance(normalized.get("status"), str):
            normalized["status"] = enum_maps["activity_status"].get(
                normalized["status"], normalized["status"]
            )
        if isinstance(normalized.get("outcome"), str):
            normalized["outcome"] = enum_maps["activity_outcome"].get(
                normalized["outcome"], normalized["outcome"]
            )
    if event_type == "stream.snapshot":
        snapshot = normalized.get("snapshot", normalized)
        if isinstance(snapshot, dict):
            for envelope_field in ("session_id", "turn_id", "turn_stream_id", "job_id"):
                snapshot.pop(envelope_field, None)
            if isinstance(snapshot.get("stream_status"), str):
                snapshot["stream_status"] = enum_maps["status"].get(
                    snapshot["stream_status"], snapshot["stream_status"]
                )
            failure = snapshot.get("failure")
            if isinstance(failure, dict):
                # model.failed 事件历史上会把内部模型错误暂存到顶层
                # failure；公共快照这里实际要求的是 StreamFailure，不能
                # 把 model_call_id、attempt 等内部字段直接交给 protobuf。
                public_failure = {
                    key: failure[key]
                    for key in (
                        "code",
                        "message",
                        "after_interrupt_requested",
                        "resumable",
                    )
                    if key in failure
                }
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
                if (
                    isinstance(execution, dict)
                    and isinstance(execution.get("status"), str)
                ):
                    if execution["status"] == "outcome_unknown":
                        execution["status"] = "completed"
                        execution.setdefault("outcome", "outcome_unknown")
                    execution["status"] = enum_maps["status_tool"].get(
                        execution["status"], execution["status"]
                    )
                if isinstance(execution, dict) and isinstance(execution.get("outcome"), str):
                    execution["outcome"] = enum_maps["outcome_tool"].get(
                        execution["outcome"], execution["outcome"]
                    )
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
                    # tool-call 聚合会保留旧版本的内部错误详情，但公共
                    # ToolCall 没有 error 字段；终态 StreamFailure 承载对用户
                    # 可见的失败原因，不能让旧字段把整个 snapshot 编码成 500。
                    tool_call.pop("error", None)
            for activity in snapshot.get("activities", []):
                if isinstance(activity, dict):
                    # Activity 的完成原因属于内部诊断字段，公共快照只投影
                    # schema 已声明的生命周期、结果和时间字段。
                    activity.pop("completion_reason", None)
                    if isinstance(activity.get("status"), str):
                        activity["status"] = enum_maps["activity_status"].get(
                            activity["status"], activity["status"]
                        )
                    if isinstance(activity.get("outcome"), str):
                        activity["outcome"] = enum_maps["activity_outcome"].get(
                            activity["outcome"], activity["outcome"]
                        )
            for block in snapshot.get("blocks", []):
                if isinstance(block, dict):
                    # model_call_id 是内部 checkpoint 的对账字段，公共
                    # MessageBlockSnapshot 只暴露 projection，避免把 attempt
                    # 归属重复塞进 block payload。
                    block.pop("model_call_id", None)
    return normalized


def _denormalize_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    status_map = {
        "STREAM_STATUS_OPEN": "open",
        "STREAM_STATUS_INTERRUPTING": "interrupting",
        "STREAM_STATUS_COMPLETED": "completed",
        "STREAM_STATUS_INTERRUPTED": "interrupted",
        "STREAM_STATUS_FAILED": "failed",
    }
    outcome_map = {
        "MODEL_CALL_OUTCOME_ACCEPTED": "accepted",
        "MODEL_CALL_OUTCOME_VALIDATION_FAILED": "validation_failed",
        "MODEL_CALL_OUTCOME_UPSTREAM_ERROR": "upstream_error",
        "MODEL_CALL_OUTCOME_EXECUTION_LOST": "execution_lost",
        "MODEL_CALL_OUTCOME_USER_INTERRUPT": "user_interrupt",
    }
    operation_map = {
        "BLOCK_DELTA_OPERATION_APPEND": "append",
        "BLOCK_DELTA_OPERATION_ITEM_UPSERT": "item_upsert",
        "BLOCK_DELTA_OPERATION_ITEM_PATCH": "item_patch",
        "BLOCK_DELTA_OPERATION_REDACTED": "redacted",
    }
    tool_status_map = {
        "TOOL_EXECUTION_STATUS_RUNNING": "running",
        "TOOL_EXECUTION_STATUS_COMPLETED": "completed",
        "TOOL_EXECUTION_STATUS_FAILED": "failed",
    }
    tool_outcome_map = {
        "TOOL_EXECUTION_OUTCOME_SUCCESS": "success",
        "TOOL_EXECUTION_OUTCOME_PROVIDER_ERROR": "provider_error",
        "TOOL_EXECUTION_OUTCOME_EXECUTION_LOST": "execution_lost",
        "TOOL_EXECUTION_OUTCOME_OUTCOME_UNKNOWN": "outcome_unknown",
    }
    if (
        event_type in {"stream.opened", "stream.completed", "stream.interrupted"}
        and isinstance(payload.get("status"), str)
    ):
        payload["status"] = status_map.get(payload["status"], payload["status"])
    if (
        event_type in {"model.completed", "model.failed"}
        and isinstance(payload.get("outcome"), str)
    ):
        payload["outcome"] = outcome_map.get(
            payload["outcome"], payload["outcome"]
        )
    if event_type == "block.delta" and isinstance(payload.get("operation"), str):
        payload["operation"] = operation_map.get(
            payload["operation"], payload["operation"]
        )
    if event_type == "tool.completed" and isinstance(payload.get("status"), str):
        payload["status"] = tool_status_map.get(payload["status"], payload["status"])
    if event_type == "tool.completed" and isinstance(payload.get("outcome"), str):
        payload["outcome"] = tool_outcome_map.get(payload["outcome"], payload["outcome"])
    if event_type.startswith("activity."):
        activity_status_map = {
            "ACTIVITY_STATUS_RUNNING": "running",
            "ACTIVITY_STATUS_WAITING": "waiting",
            "ACTIVITY_STATUS_STOPPING": "stopping",
            "ACTIVITY_STATUS_COMPLETED": "completed",
            "ACTIVITY_STATUS_FAILED": "failed",
            "ACTIVITY_STATUS_UNKNOWN": "unknown",
        }
        activity_outcome_map = {
            "ACTIVITY_OUTCOME_SUCCESS": "success",
            "ACTIVITY_OUTCOME_USER_INTERRUPT": "user_interrupt",
            "ACTIVITY_OUTCOME_PROVIDER_ERROR": "provider_error",
            "ACTIVITY_OUTCOME_EXECUTION_LOST": "execution_lost",
            "ACTIVITY_OUTCOME_OUTCOME_UNKNOWN": "outcome_unknown",
        }
        if isinstance(payload.get("status"), str):
            payload["status"] = activity_status_map.get(
                payload["status"], payload["status"]
            )
        if isinstance(payload.get("outcome"), str):
            payload["outcome"] = activity_outcome_map.get(
                payload["outcome"], payload["outcome"]
            )
    return payload


def _denormalize_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    status_map = {
        "STREAM_STATUS_OPEN": "open",
        "STREAM_STATUS_INTERRUPTING": "interrupting",
        "STREAM_STATUS_COMPLETED": "completed",
        "STREAM_STATUS_INTERRUPTED": "interrupted",
        "STREAM_STATUS_FAILED": "failed",
    }
    tool_status_map = {
        "TOOL_EXECUTION_STATUS_RUNNING": "running",
        "TOOL_EXECUTION_STATUS_COMPLETED": "completed",
        "TOOL_EXECUTION_STATUS_FAILED": "failed",
    }
    activity_status_map = {
        "ACTIVITY_STATUS_RUNNING": "running",
        "ACTIVITY_STATUS_WAITING": "waiting",
        "ACTIVITY_STATUS_STOPPING": "stopping",
        "ACTIVITY_STATUS_COMPLETED": "completed",
        "ACTIVITY_STATUS_FAILED": "failed",
        "ACTIVITY_STATUS_UNKNOWN": "unknown",
    }
    activity_outcome_map = {
        "ACTIVITY_OUTCOME_SUCCESS": "success",
        "ACTIVITY_OUTCOME_USER_INTERRUPT": "user_interrupt",
        "ACTIVITY_OUTCOME_PROVIDER_ERROR": "provider_error",
        "ACTIVITY_OUTCOME_EXECUTION_LOST": "execution_lost",
        "ACTIVITY_OUTCOME_OUTCOME_UNKNOWN": "outcome_unknown",
    }
    tool_outcome_map = {
        "TOOL_EXECUTION_OUTCOME_SUCCESS": "success",
        "TOOL_EXECUTION_OUTCOME_PROVIDER_ERROR": "provider_error",
        "TOOL_EXECUTION_OUTCOME_EXECUTION_LOST": "execution_lost",
        "TOOL_EXECUTION_OUTCOME_OUTCOME_UNKNOWN": "outcome_unknown",
    }
    if isinstance(payload.get("stream_status"), str):
        payload["stream_status"] = status_map.get(
            payload["stream_status"], payload["stream_status"]
        )
    snapshot_seq = payload.get("snapshot_seq")
    if isinstance(snapshot_seq, str) and snapshot_seq.isdecimal():
        payload["snapshot_seq"] = int(snapshot_seq)
    for entity_collection in (
        "blocks",
        "tool_calls",
        "tool_executions",
        "model_calls",
        "activities",
    ):
        for entity in payload.get(entity_collection, []):
            if not isinstance(entity, dict):
                continue
            for field_name in ("started_seq", "last_event_seq", "completed_seq"):
                sequence = entity.get(field_name)
                if isinstance(sequence, str) and sequence.isdecimal():
                    entity[field_name] = int(sequence)
    interrupt_request_id = payload.pop("interrupt_request_id", None)
    interrupt_status = payload.pop("interrupt_status", None)
    if interrupt_request_id is not None or interrupt_status is not None:
        payload["interrupt_state"] = {
            "request_id": interrupt_request_id,
            "status": interrupt_status,
        }
    for execution in payload.get("tool_executions", []):
        if isinstance(execution, dict) and isinstance(execution.get("status"), str):
            execution["status"] = tool_status_map.get(
                execution["status"], execution["status"]
            )
        if isinstance(execution, dict) and isinstance(execution.get("outcome"), str):
            execution["outcome"] = tool_outcome_map.get(
                execution["outcome"], execution["outcome"]
            )
    for activity in payload.get("activities", []):
        if not isinstance(activity, dict):
            continue
        if isinstance(activity.get("status"), str):
            activity["status"] = activity_status_map.get(
                activity["status"], activity["status"]
            )
        if isinstance(activity.get("outcome"), str):
            activity["outcome"] = activity_outcome_map.get(
                activity["outcome"], activity["outcome"]
            )
    return payload
