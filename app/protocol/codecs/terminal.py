from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from google.protobuf.descriptor import FieldDescriptor

from app.protocol.codecs.json import struct_from_mapping, struct_to_mapping
from app.protocol.generated.boxteam.terminal.v1 import terminal_pb2

_TERMINAL_STATUS_ALIASES = {
    # Terminal Manager 会在命令完成、隐藏 PTY 释放完成前短暂报告 completed。
    # TODO: 兼容 Terminal Manager 的命令完成中间态；协议枚举仍以 exited 表示
    # 进程生命周期结束，原始状态通过 metadata 保留给上层业务。
    "completed": terminal_pb2.TERMINAL_STATUS_EXITED,
}


def _status(value: object) -> terminal_pb2.TerminalStatus:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Terminal 状态必须是非空字符串: {value!r}")
    enum_value = _TERMINAL_STATUS_ALIASES.get(value)
    if enum_value is None:
        enum_value = getattr(terminal_pb2, f"TERMINAL_STATUS_{value.upper()}", None)
    if not isinstance(enum_value, int):
        raise TypeError(f"Terminal 状态不在协议范围内: {value}")
    return cast(terminal_pb2.TerminalStatus, enum_value)


def terminal_session_to_proto(value: Mapping[str, object]) -> terminal_pb2.TerminalSession:
    required = ("terminal_id", "workspace_id", "status")
    for field_name in required:
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise ValueError(f"Terminal 会话缺少字段: {field_name}")
    message = terminal_pb2.TerminalSession(
        terminal_id=cast(str, value["terminal_id"]),
        workspace_id=cast(str, value["workspace_id"]),
        status=_status(value["status"]),
    )
    # 同形标量及整数的符号由 descriptor 推导；必填身份和状态别名仍由此边界核定。
    for field in message.DESCRIPTOR.fields:
        field_value = value.get(field.name)
        if field_value is None or field.name in required:
            continue
        if field.type == FieldDescriptor.TYPE_STRING:
            if not isinstance(field_value, str):
                raise ValueError(f"Terminal 会话字段类型错误: {field.name}")
            setattr(message, field.name, field_value)
        elif field.type == FieldDescriptor.TYPE_INT32:
            if not isinstance(field_value, int):
                raise ValueError(f"Terminal 会话退出码必须是整数: {field.name}")
            setattr(message, field.name, field_value)
        elif field.type in (FieldDescriptor.TYPE_UINT32, FieldDescriptor.TYPE_UINT64):
            if not isinstance(field_value, int) or field_value < 0:
                raise ValueError(f"Terminal 会话字段必须是非负整数: {field.name}")
            setattr(message, field.name, field_value)
    message.metadata.CopyFrom(struct_from_mapping({"raw": dict(value)}))
    return message


def terminal_session_to_json(value: terminal_pb2.TerminalSession) -> dict[str, object]:
    if not value.HasField("metadata"):
        raise ValueError(f"Terminal Protobuf 会话缺少 raw metadata: terminal_id={value.terminal_id}")
    raw = struct_to_mapping(value.metadata).get("raw")
    if not isinstance(raw, dict):
        raise TypeError(f"Terminal Protobuf 会话 raw metadata 格式错误: terminal_id={value.terminal_id}")
    result = cast(dict[str, object], raw)
    # Struct 的 JSON 数值会解为 float；transport fence 使用官方 uint64 字段。
    result["transport_owner_revision"] = value.transport_owner_revision
    for field, field_value in value.ListFields():
        if field.has_presence and field.type in (FieldDescriptor.TYPE_INT32, FieldDescriptor.TYPE_UINT32, FieldDescriptor.TYPE_UINT64):
            result[field.name] = field_value
    return result
