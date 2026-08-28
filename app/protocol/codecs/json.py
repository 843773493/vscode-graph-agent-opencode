from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TypeVar, cast

from google.protobuf import json_format
from google.protobuf.message import Message
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp

ProtoMessageT = TypeVar("ProtoMessageT", bound=Message)


def message_to_json(message: Message) -> dict[str, object]:
    """把 Protobuf 消息转换为保留 snake_case 字段名的 JSON 对象。"""

    return cast(
        dict[str, object],
        json_format.MessageToDict(
            message,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=False,
        ),
    )


def message_from_json(
    message_type: type[ProtoMessageT],
    payload: Mapping[str, object],
) -> ProtoMessageT:
    """严格从 JSON 对象解析消息，未知字段直接报错。"""

    message = message_type()
    json_format.ParseDict(dict(payload), message, ignore_unknown_fields=False)
    return message


def struct_from_mapping(payload: Mapping[str, object]) -> Struct:
    struct = Struct()
    json_format.ParseDict(dict(payload), struct, ignore_unknown_fields=False)
    return struct


def struct_to_mapping(payload: Struct) -> dict[str, object]:
    return message_to_json(payload)


def timestamp_from_datetime(value: datetime) -> Timestamp:
    if value.tzinfo is None:
        raise ValueError("Protobuf Timestamp 不接受无时区 datetime")
    timestamp = Timestamp()
    timestamp.FromDatetime(value.astimezone(UTC))
    return timestamp


def datetime_from_timestamp(value: Timestamp) -> datetime:
    return value.ToDatetime(UTC)
