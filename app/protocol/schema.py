"""由 descriptor 核对公开 JSON 结构，业务约束仍由内部模型负责。"""

from __future__ import annotations

from collections.abc import Callable

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.descriptor_pb2 import FileDescriptorSet
from google.protobuf.descriptor_pool import DescriptorPool
from pydantic.json_schema import JsonSchemaValue

from app.protocol.codecs.json import enum_json_names


def descriptor_pool_from_set(data: bytes) -> DescriptorPool:
    """消费本次编译的 descriptor，避免旧 binding 掩盖源协议漂移。"""
    pool = DescriptorPool()
    # Buf build 输出按依赖顺序排列；缺少依赖时由 protobuf 明确拒绝。
    for file in FileDescriptorSet.FromString(data).file:
        pool.Add(file)
    return pool


def assert_public_schema(
    schema: JsonSchemaValue, descriptor: Descriptor, *,
    message_adapter: Callable[[JsonSchemaValue, Descriptor], JsonSchemaValue | None] | None = None,
) -> None:
    """核对结构；具名 adapter 可投影外形或接管异形消息，业务约束仍由模型负责。"""
    visited: set[tuple[str, int]] = set()

    def concrete(node: JsonSchemaValue, path: str) -> JsonSchemaValue:
        if "$ref" in node:
            target = schema
            for part in node["$ref"].removeprefix("#/").split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return concrete(target, path)
        return node

    def variants(node: JsonSchemaValue, path: str) -> list[JsonSchemaValue]:
        node = concrete(node, path)
        branches = node.get("anyOf", node.get("oneOf"))
        if branches is not None:
            values = [branch for branch in branches if branch.get("type") != "null"]
            if not values:
                raise ValueError(f"公开结构不能机械对应 proto: {path} 只有空值分支")
            return [value for branch in values for value in variants(branch, path)]
        return [node]

    def require_type(node: JsonSchemaValue, expected: str, path: str) -> None:
        for value in variants(node, path):
            actual = value.get("type")
            if actual != expected and not (expected == "number" and actual == "integer"):
                raise ValueError(f"公开结构与 proto 类型不一致: {path}, expected={expected}, actual={actual}")

    def field_shape(node: JsonSchemaValue, field: FieldDescriptor, path: str) -> None:
        if field.is_repeated:
            message = field.message_type
            if message is not None and message.GetOptions().map_entry:
                require_type(node, "object", path)
                for value in variants(node, path):
                    field_shape(value["additionalProperties"], message.fields_by_name["value"], path + "/*")
            else:
                require_type(node, "array", path)
                for value in variants(node, path):
                    value_shape(value["items"], field, path + "/*")
        else:
            value_shape(node, field, path)

    def value_shape(node: JsonSchemaValue, field: FieldDescriptor, path: str) -> None:
        if field.message_type is not None:
            special = {"google.protobuf.Timestamp": "string", "google.protobuf.Struct": "object",
                       "google.protobuf.ListValue": "array"}.get(field.message_type.full_name)
            if special is not None:
                require_type(node, special, path)
            elif field.message_type.full_name != "google.protobuf.Value":
                message_shape(node, field.message_type, path)
        elif field.enum_type is not None:
            require_type(node, "string", path)
            names = enum_json_names(field.enum_type, to_proto=True)
            for value in variants(node, path):
                declared = value.get("enum", [value["const"]] if "const" in value else None)
                if declared is None:
                    raise ValueError(f"公开结构与 proto 枚举不一致: {path}, 缺少明确的 enum/const 值域")
                extra = sorted(set(declared) - names.keys())
                if extra:
                    raise ValueError(f"公开结构与 proto 枚举不一致: {path}, unsupported={extra}")
        elif field.type in (FieldDescriptor.TYPE_STRING, FieldDescriptor.TYPE_BYTES):
            require_type(node, "string", path)
        elif field.type == FieldDescriptor.TYPE_BOOL:
            require_type(node, "boolean", path)
        elif field.type in (FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE):
            require_type(node, "number", path)
        else:
            require_type(node, "integer", path)

    def message_shape(node: JsonSchemaValue, message: Descriptor, path: str) -> None:
        if message_adapter is not None:
            adapted = message_adapter(concrete(node, path), message)
            if adapted is None:
                return
            node = adapted
        values = variants(node, path)
        for value in values:
            require_type(value, "object", path)
        public_fields = set().union(*(value.get("properties", {}) for value in values))
        missing = sorted(set(message.fields_by_name) - public_fields)
        extra = sorted(public_fields - set(message.fields_by_name))
        if missing or extra:
            raise ValueError(f"公开结构与 proto 字段不一致: {path}, message={message.full_name}, missing={missing}, extra={extra}")
        for value in values:
            key = (message.full_name, id(value))
            if key in visited:
                continue
            visited.add(key)
            properties = value.get("properties", {})
            # 业务联合可按分支省略有 presence 的字段；其余公共字段不能消失。
            missing_common = sorted(field.name for field in message.fields
                                    if field.name not in properties and not field.has_presence)
            if missing_common:
                raise ValueError(f"公开结构与 proto 联合分支字段不一致: {path}, missing={missing_common}")
            for name, field_schema in properties.items():
                field_shape(field_schema, message.fields_by_name[name], path + "/" + name)

    message_shape(schema, descriptor, descriptor.full_name)
