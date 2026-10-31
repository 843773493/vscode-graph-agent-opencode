from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from google.protobuf.descriptor import Descriptor
from pydantic.json_schema import JsonSchemaValue

from app.protocol.codecs.json import event_payload_fields
from app.protocol.generated.boxteam.workspace.v2 import (
    session_interaction_pb2,
    session_stream_pb2,
)
from app.protocol.openapi import SSE_JSON_DESCRIPTORS
from app.protocol.schema import assert_public_schema, descriptor_pool_from_set
from app.schemas.internal_v2.session_interaction import SessionExecutionSseDTO
from app.schemas.internal_v2.sse import SSE_EVENT_MODELS


def assert_session_sse_schema(schema: JsonSchemaValue, descriptor: Descriptor) -> None:
    """唯一投影负责展平 header 和具名 payload，外层信封仍按真实 wire 核对。"""
    event = descriptor.fields_by_name.get("event")
    if (event is None or event.message_type is None
            or event.message_type.full_name != session_interaction_pb2.SessionExecutionEvent.DESCRIPTOR.full_name):
        raise ValueError("公开 SSE 信封 event 必须对应 SessionExecutionEvent")

    def project(node: JsonSchemaValue, message: Descriptor) -> JsonSchemaValue | None:
        if message.full_name == session_interaction_pb2.SessionStatus.DESCRIPTOR.full_name:
            # 状态联合由既有业务 adapter 转换，不声明其 JSON 与 wire 同形。
            return None
        if message.full_name != session_interaction_pb2.SessionExecutionEvent.DESCRIPTOR.full_name:
            return node
        payloads = event_payload_fields(message)
        discriminator = node["discriminator"]
        mapping = discriminator["mapping"]
        if discriminator["propertyName"] != "type" or payloads.keys() != mapping.keys():
            raise ValueError(f"公开 SSE 事件与 proto 分支不一致: "
                             f"missing={sorted(payloads.keys() - mapping.keys())}, "
                             f"extra={sorted(mapping.keys() - payloads.keys())}")
        if {branch["$ref"] for branch in node["oneOf"]} != set(mapping.values()):
            raise ValueError("公开 SSE 事件分支与 discriminator 不一致")
        header = message.fields_by_name["header"].message_type
        if header is None:
            raise ValueError("公开 SSE 事件 header 必须为消息")
        common = set(message.fields_by_name) - {field.name for field in payloads.values()} - {"header"}
        branches = []
        for tag, reference in mapping.items():
            field = payloads[tag]
            if field.message_type is None:
                raise ValueError(f"公开 SSE 事件 payload 必须为消息: {tag}")
            if (tag == "session.status.changed"
                    and field.message_type.full_name != session_interaction_pb2.SessionStatus.DESCRIPTOR.full_name):
                raise ValueError(f"公开 SSE 事件 payload 与业务 adapter 类型不一致: {tag}")
            properties = schema["$defs"][reference.rsplit("/", 1)[1]]["properties"]
            expected = common | set(header.fields_by_name) | {"payload"}
            if set(properties) != expected:
                raise ValueError(f"公开结构与 proto 字段不一致: {message.full_name}, event={tag}, "
                                 f"missing={sorted(expected - properties.keys())}, "
                                 f"extra={sorted(properties.keys() - expected)}")
            declared = properties["type"]
            labels = declared.get("enum", [declared["const"]] if "const" in declared else [])
            if (set(labels) != {name for name, target in mapping.items() if target == reference}
                    or declared.get("type") != "string"):
                raise ValueError(f"公开 SSE 事件标签与 discriminator 不一致: {tag}")
            branches.append({"type": "object", "properties": {
                **{name: properties[name] for name in common},
                "header": {"type": "object", "properties": {
                    name: properties[name] for name in header.fields_by_name
                }}, field.name: properties["payload"],
            }})
        return {"oneOf": branches}

    assert_public_schema(schema, descriptor, message_adapter=project)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 SSE 校验 schema，并由 proto 核对同形公开结构")
    parser.add_argument("--output-root", type=Path, default=Path.cwd())
    parser.add_argument("--descriptor-set", type=Path)
    args = parser.parse_args()
    project_root = Path.cwd()
    if not (project_root / "pyproject.toml").is_file():
        raise RuntimeError(f"必须从项目根目录运行: cwd={project_root}")
    pool = descriptor_pool_from_set(args.descriptor_set.read_bytes()) if args.descriptor_set else None
    schemas: dict[str, object] = {}
    checked: set[str] = set()
    for model in SSE_EVENT_MODELS.values():
        schema = model.model_json_schema(mode="serialization")
        definitions = schema.get("$defs", {})
        if model is SessionExecutionSseDTO:
            envelope = cast(Descriptor, session_stream_pb2.SessionExecutionSse.DESCRIPTOR)
            if pool is not None:
                envelope = pool.FindMessageTypeByName(envelope.full_name)
            assert_session_sse_schema(schema, envelope)
        for name, shape in [(model.__name__, schema), *definitions.items()]:
            descriptor = SSE_JSON_DESCRIPTORS.get(name)
            if descriptor is None:
                continue
            if pool is not None:
                descriptor = pool.FindMessageTypeByName(descriptor.full_name)
            assert_public_schema({**shape, "$defs": definitions}, descriptor)
            checked.add(name)
            if name == model.__name__:
                schema["x-protobuf-message"] = descriptor.full_name
        schema["$id"] = f"urn:boxteam:sse:{model.__name__}"
        schemas[model.__name__] = schema
    missing = sorted(set(SSE_JSON_DESCRIPTORS) - checked)
    if missing:
        raise ValueError(f"公开 SSE 结构未被导出或校验: {missing}")
    payload = {"generated": True, "schemas": schemas}
    target = (
        args.output_root
        / "src"
        / "clients"
        / "web"
        / "src"
        / "types"
        / "protocol_generated"
        / "sse_runtime_schemas.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
