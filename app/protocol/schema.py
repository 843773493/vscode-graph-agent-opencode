"""由 descriptor 核对公开 JSON 结构，业务约束仍由内部模型负责。"""

from __future__ import annotations

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from pydantic.json_schema import JsonSchemaValue


def assert_public_schema(schema: JsonSchemaValue, descriptor: Descriptor) -> None:
    """检查同形 JSON 的字段、容器和类型，允许业务模型收紧取值与 required。"""
    visited: set[tuple[str, int]] = set()

    def concrete(node: JsonSchemaValue, path: str) -> JsonSchemaValue:
        if "$ref" in node:
            target = schema
            for part in node["$ref"].removeprefix("#/").split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return concrete(target, path)
        branches = node.get("anyOf", node.get("oneOf"))
        if branches is not None:
            values = [branch for branch in branches if branch.get("type") != "null"]
            if len(values) != 1:
                raise ValueError(f"公开结构不能机械对应 proto: {path} 含多个非空分支")
            return concrete(values[0], path)
        return node

    def require_type(node: JsonSchemaValue, expected: str, path: str) -> None:
        actual = node.get("type")
        if actual != expected and not (expected == "number" and actual == "integer"):
            raise ValueError(f"公开结构与 proto 类型不一致: {path}, expected={expected}, actual={actual}")

    def field_shape(node: JsonSchemaValue, field: FieldDescriptor, path: str) -> None:
        node = concrete(node, path)
        if field.is_repeated:
            is_map = field.message_type is not None and field.message_type.GetOptions().map_entry
            require_type(node, "object" if is_map else "array", path)
            if is_map:
                field_shape(node["additionalProperties"], field.message_type.fields_by_name["value"], path + "/*")
                return
            node = concrete(node["items"], path + "/*")
        if field.message_type is not None:
            special = {"google.protobuf.Timestamp": "string", "google.protobuf.Struct": "object",
                       "google.protobuf.ListValue": "array"}.get(field.message_type.full_name)
            if special is not None:
                require_type(node, special, path)
            elif field.message_type.full_name != "google.protobuf.Value":
                message_shape(node, field.message_type, path)
        elif field.type in (FieldDescriptor.TYPE_STRING, FieldDescriptor.TYPE_BYTES, FieldDescriptor.TYPE_ENUM):
            require_type(node, "string", path)
        elif field.type == FieldDescriptor.TYPE_BOOL:
            require_type(node, "boolean", path)
        elif field.type in (FieldDescriptor.TYPE_FLOAT, FieldDescriptor.TYPE_DOUBLE):
            require_type(node, "number", path)
        else:
            require_type(node, "integer", path)

    def message_shape(node: JsonSchemaValue, message: Descriptor, path: str) -> None:
        node = concrete(node, path)
        key = (message.full_name, id(node))
        if key in visited:
            return
        visited.add(key)
        require_type(node, "object", path)
        properties = node.get("properties", {})
        missing = sorted(set(message.fields_by_name) - set(properties))
        extra = sorted(set(properties) - set(message.fields_by_name))
        if missing or extra:
            raise ValueError(f"公开结构与 proto 字段不一致: {path}, missing={missing}, extra={extra}")
        for field in message.fields:
            field_shape(properties[field.name], field, path + "/" + field.name)

    message_shape(schema, descriptor, descriptor.full_name)
