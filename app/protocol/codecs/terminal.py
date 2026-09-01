from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from app.protocol.codecs.json import struct_from_mapping, struct_to_mapping
from app.protocol.generated.boxteam.terminal.v1 import terminal_pb2

_TERMINAL_STATUS_ALIASES = {
    # Terminal Manager 会在命令完成、隐藏 PTY 释放完成前短暂报告 completed。
    # TODO: 兼容 Terminal Manager 的命令完成中间态；协议枚举仍以 exited 表示
    # 进程生命周期结束，原始状态通过 metadata 保留给上层业务。
    "completed": terminal_pb2.TERMINAL_STATUS_EXITED,
}


def _status(value: object) -> int:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Terminal 状态必须是非空字符串: {value!r}")
    enum_value = _TERMINAL_STATUS_ALIASES.get(value)
    if enum_value is None:
        enum_value = getattr(terminal_pb2, f"TERMINAL_STATUS_{value.upper()}", None)
    if not isinstance(enum_value, int):
        raise TypeError(f"Terminal 状态不在协议范围内: {value}")
    return enum_value


def terminal_session_to_proto(value: Mapping[str, object]) -> terminal_pb2.TerminalSession:
    required = ("terminal_id", "session_id", "status")
    for field_name in required:
        if not isinstance(value.get(field_name), str) or not value[field_name]:
            raise ValueError(f"Terminal 会话缺少字段: {field_name}")
    message = terminal_pb2.TerminalSession(
        terminal_id=cast(str, value["terminal_id"]),
        session_id=cast(str, value["session_id"]),
        status=_status(value["status"]),
    )
    for field_name in ("title", "cwd"):
        field_value = value.get(field_name)
        if field_value is not None:
            if not isinstance(field_value, str):
                raise ValueError(f"Terminal 会话字段类型错误: {field_name}")
            setattr(message, field_name, field_value)
    for field_name in ("cols", "rows", "sequence"):
        field_value = value.get(field_name)
        if field_value is not None:
            if not isinstance(field_value, int) or field_value < 0:
                raise ValueError(f"Terminal 会话字段必须是非负整数: {field_name}")
            setattr(message, field_name, field_value)
    message.metadata.CopyFrom(struct_from_mapping({"raw": dict(value)}))
    return message


def terminal_session_to_json(value: terminal_pb2.TerminalSession) -> dict[str, object]:
    if not value.HasField("metadata"):
        raise ValueError(f"Terminal Protobuf 会话缺少 raw metadata: terminal_id={value.terminal_id}")
    raw = struct_to_mapping(value.metadata).get("raw")
    if not isinstance(raw, dict):
        raise TypeError(f"Terminal Protobuf 会话 raw metadata 格式错误: terminal_id={value.terminal_id}")
    return cast(dict[str, object], raw)
