from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from google.protobuf import json_format

from app.core.identifier import validate_session_id, validate_thread_id
from app.protocol.codecs.json import struct_from_mapping, struct_to_mapping
from app.protocol.generated.boxteam.workspace.v2 import file_events_pb2, trace_pb2
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryRevisionDTO,
    WorkspaceFileListDTO,
)


def file_change_batch_to_proto(
    value: Mapping[str, object],
) -> file_events_pb2.WorkspaceFileChangeBatch:
    changes = value.get("changes")
    overflow = value.get("overflow")
    if not isinstance(changes, list) or not isinstance(overflow, bool):
        raise TypeError("Workspace 文件变更批次必须包含 changes 和 overflow")
    result = file_events_pb2.WorkspaceFileChangeBatch(overflow=overflow)
    for item in changes:
        if not isinstance(item, Mapping):
            raise TypeError("Workspace 文件变更必须是对象")
        kind = item.get("kind")
        path = item.get("path")
        if not isinstance(kind, str) or not isinstance(path, str) or not path:
            raise ValueError("Workspace 文件变更缺少 kind/path")
        result.changes.add(kind=kind, path=path)
    directories = value.get("directories", [])
    if not isinstance(directories, list):
        raise TypeError("Workspace 文件目录 revision 必须为数组")
    for item in directories:
        revision = WorkspaceDirectoryRevisionDTO.model_validate(item)
        target = result.directories.add(directory_revision=revision.directory_revision, exists=revision.exists)
        for name, field in revision.directory_key.model_dump().items():
            setattr(target.directory_key, name, field)
    event_id = value.get("event_id", 0)
    if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 0:
        raise ValueError("Workspace 文件事件 event_id 必须是非负整数")
    result.event_id = event_id
    roots = value.get("invalidated_roots", [])
    if not isinstance(roots, list) or not all(isinstance(root, str) and root for root in roots):
        raise ValueError("Workspace 文件失效根必须是非空路径数组")
    result.invalidated_roots.extend(roots)
    snapshots = value.get("snapshots", [])
    if not isinstance(snapshots, list):
        raise TypeError("Workspace 文件目录快照必须为数组")
    for snapshot in snapshots:
        listing = WorkspaceFileListDTO.model_validate(snapshot)
        json_format.ParseDict(listing.model_dump(), result.snapshots.add(), ignore_unknown_fields=False)
    return result


def file_change_batch_to_json(
    value: file_events_pb2.WorkspaceFileChangeBatch,
) -> dict[str, object]:
    return {
        "changes": [
            {"kind": change.kind, "path": change.path}
            for change in value.changes
        ],
        "overflow": value.overflow,
        "directories": [{"directory_key": {"workspace_id": item.directory_key.workspace_id,
                                            "scope": item.directory_key.scope, "root": item.directory_key.root,
                                            "path": item.directory_key.path},
                         "directory_revision": item.directory_revision, "exists": item.exists} for item in value.directories],
        "event_id": value.event_id,
        "invalidated_roots": list(value.invalidated_roots),
        "snapshots": [WorkspaceFileListDTO.model_validate(json_format.MessageToDict(
            item, preserving_proto_field_name=True, always_print_fields_with_no_presence=True,
        )).model_dump() for item in value.snapshots],
    }


def sse_error_to_proto(message: str) -> file_events_pb2.SseError:
    if not message:
        raise ValueError("SSE 错误消息不能为空")
    return file_events_pb2.SseError(message=message)


def sse_error_to_json(value: file_events_pb2.SseError) -> dict[str, object]:
    return {"message": value.message}


def trace_to_proto(value: Mapping[str, object]) -> trace_pb2.TraceEvent:
    required = ("event_id", "session_id", "thread_id", "job_id", "type", "phase", "title", "content")
    for field_name in required:
        if not isinstance(value.get(field_name), str):
            raise TypeError(f"Trace 事件缺少字段: {field_name}")
    # 2.1：wire 上的 session_id 必须过唯一 canonical 验证器（encode 边界）。
    validate_session_id(cast(str, value["session_id"]))
    validate_thread_id(cast(str, value["thread_id"]))
    result = trace_pb2.TraceEvent(
        event_id=cast(str, value["event_id"]),
        session_id=cast(str, value["session_id"]),
        thread_id=cast(str, value["thread_id"]),
        job_id=cast(str, value["job_id"]),
        type=cast(str, value["type"]),
        phase=cast(str, value["phase"]),
        title=cast(str, value["title"]),
        content=cast(str, value["content"]),
    )
    for field_name in ("part_id", "status", "tool_name", "step_id"):
        field_value = value.get(field_name)
        if field_value is not None:
            if not isinstance(field_value, str):
                raise ValueError(f"Trace 事件字段类型错误: {field_name}")
            setattr(result, field_name, field_value)
    skill_names = value.get("skill_names", [])
    if not isinstance(skill_names, list) or not all(isinstance(item, str) for item in skill_names):
        raise ValueError("Trace 事件 skill_names 必须是字符串数组")
    result.skill_names.extend(skill_names)
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
    return {
        "event_id": value.event_id,
        "part_id": value.part_id if value.HasField("part_id") else None,
        "session_id": value.session_id,
        "thread_id": value.thread_id,
        "job_id": value.job_id,
        "type": value.type,
        "phase": value.phase,
        "title": value.title,
        "content": value.content,
        "status": value.status if value.HasField("status") else None,
        "tool_name": value.tool_name if value.HasField("tool_name") else None,
        "skill_names": list(value.skill_names),
        "step_id": value.step_id if value.HasField("step_id") else None,
        "timestamp": value.timestamp.ToJsonString(),
        "raw": struct_to_mapping(value.raw) if value.HasField("raw") else {},
    }
