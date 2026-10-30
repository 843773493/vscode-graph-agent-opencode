from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from typing import cast

from google.protobuf import json_format
from google.protobuf.descriptor import Descriptor, EnumDescriptor, FieldDescriptor
from google.protobuf.message import Message
from google.protobuf.struct_pb2 import Struct
from google.protobuf.timestamp_pb2 import Timestamp


def event_payload_fields(
    descriptor: Descriptor, *, aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, FieldDescriptor]:
    """普通事件标签由 oneof 字段推导，非同形业务别名单独声明。"""
    fields = descriptor.oneofs_by_name["payload"].fields
    aliases = aliases or {}
    unknown = sorted(aliases.keys() - {field.name for field in fields})
    if unknown:
        raise ValueError(f"事件别名没有对应的 proto payload: {descriptor.full_name}, fields={unknown}")
    result: dict[str, FieldDescriptor] = {}
    for field in fields:
        tags = aliases.get(field.name, (field.name.replace("_", "."),))
        if not tags:
            raise ValueError(f"proto payload 缺少公开事件标签: {descriptor.full_name}.{field.name}")
        for tag in tags:
            if not tag or tag in result:
                raise ValueError(f"公开事件标签为空或重复: {descriptor.full_name}.{field.name}")
            result[tag] = field
    return result


@cache
def enum_json_names(descriptor: EnumDescriptor, to_proto: bool) -> Mapping[str, str]:
    """枚举值的公共小写名称由 proto 的零值前缀推导。"""
    zero = descriptor.values_by_number[0].name
    if not zero.endswith("_UNSPECIFIED"):
        raise ValueError(f"公开枚举缺少 UNSPECIFIED 零值: {descriptor.full_name}")
    prefix = zero.removesuffix("UNSPECIFIED")
    names = {value.name.removeprefix(prefix).lower(): value.name
             for value in descriptor.values if value.number != 0}
    return names if to_proto else {value: key for key, value in names.items()}


def map_enum_values(
    payload: Mapping[str, object], descriptor: Descriptor, *, to_proto: bool,
) -> dict[str, object]:
    """按字段的真实枚举类型递归转换；Struct 正文和业务别名不参与推导。"""
    result = dict(payload)
    for field in descriptor.fields:
        value = result.get(field.name)
        if field.enum_type is not None:
            names = enum_json_names(field.enum_type, to_proto)
            if field.is_repeated and isinstance(value, list):
                result[field.name] = [names.get(item, item) if isinstance(item, str) else item for item in value]
            elif isinstance(value, str):
                result[field.name] = names.get(value, value)
        elif field.message_type is not None and not field.message_type.full_name.startswith("google.protobuf."):
            if field.message_type.GetOptions().map_entry:
                item_type = field.message_type.fields_by_name["value"].message_type
                if item_type is not None and isinstance(value, Mapping):
                    result[field.name] = {key: map_enum_values(item, item_type, to_proto=to_proto)
                                          if isinstance(item, Mapping) else item for key, item in value.items()}
            elif field.is_repeated and isinstance(value, list):
                result[field.name] = [map_enum_values(item, field.message_type, to_proto=to_proto)
                                      if isinstance(item, Mapping) else item for item in value]
            elif isinstance(value, Mapping):
                result[field.name] = map_enum_values(value, field.message_type, to_proto=to_proto)
    return result


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
