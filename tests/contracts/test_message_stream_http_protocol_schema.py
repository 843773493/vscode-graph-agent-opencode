"""消息流同形公开对象从 descriptor 核对，身份与序号约束仍归 Pydantic。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import (
    MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS,
    assert_public_http_schemas,
)
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.internal_v2.message_stream import (
    MessageStreamBlockSnapshotDTO,
    MessageStreamSnapshotDTO,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


def test_snapshot_http_route_checks_all_same_shape_members(http_openapi: JsonSchemaValue) -> None:
    original = deepcopy(http_openapi)
    route = "/api/v1/workspaces/{workspace_id}/sessions/{session_id}/turns/{turn_id}/message-stream/snapshot"
    response = http_openapi["paths"][route]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    schemas = http_openapi["components"]["schemas"]
    envelope = schemas[response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/MessageStreamSnapshotDTO")
    members = schemas["MessageStreamSnapshotDTO"]["properties"]
    references = str(members)
    assert all(name in references for name in MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("MessageStreamBlockSnapshotDTO", "model_call_id", {"type": "integer"}),
    ("MessageStreamBlockSnapshotDTO", "items", {"type": "object"}),
    ("MessageStreamToolCallDTO", "arguments", {"type": "string"}),
    ("MessageStreamModelCallSnapshotDTO", "completed_seq", {"type": "string"}),
    ("MessageStreamToolExecutionSnapshotDTO", "status", {"type": "string"}),
    ("MessageStreamActivityDTO", "resource_refs", {"type": "array", "items": {"type": "integer"}}),
])
def test_snapshot_http_rejects_member_type_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto") as captured:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(captured.value)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_snapshot_http_rejects_block_identity_field_drift(http_openapi: JsonSchemaValue, mutation: str) -> None:
    properties = http_openapi["components"]["schemas"]["MessageStreamBlockSnapshotDTO"]["properties"]
    if mutation == "missing":
        del properties["model_call_id"]
    else:
        properties["internal_field"] = {"type": "string"}
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致"):
        assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize("model", MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS)
def test_snapshot_http_checks_fresh_descriptor_members(
    http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    descriptor = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString()).FindMessageTypeByName(MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS[model])
    file = next(file for file in http_descriptor_bundle.file if file.name == descriptor.file.name)
    message = next(message for message in file.message_type if message.name == descriptor.name)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as captured:
        assert_public_http_schemas(http_openapi, descriptor_pool_from_set(http_descriptor_bundle.SerializeToString()))
    assert "new_public_field" in str(captured.value)
    assert descriptor.full_name in str(captured.value)


@pytest.mark.parametrize("model", ["MessageStreamBlockSnapshotDTO", "MessageStreamActivityDTO"])
def test_snapshot_descriptor_drift_rejects_publication(
    http_descriptor_bundle: FileDescriptorSet, tmp_path: Path, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    descriptor = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString()).FindMessageTypeByName(MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS[model])
    file = next(file for file in http_descriptor_bundle.file if file.name == descriptor.file.name)
    message = next(message for message in file.message_type if message.name == descriptor.name)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    candidate = tmp_path / "candidate.binpb"
    candidate.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as captured:
        openapi_export(openapi_output, candidate)
    assert "new_public_field" in str(captured.value)
    assert descriptor.full_name in str(captured.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


@pytest.mark.parametrize("invalid", [
    {"block_id": ""}, {"model_call_id": ""}, {"started_seq": -1}, {"extra_identity": "wrong"},
])
def test_snapshot_structure_checks_keep_business_constraints(http_openapi: JsonSchemaValue, invalid: dict[str, object]) -> None:
    assert_public_http_schemas(http_openapi)
    with pytest.raises(ValidationError):
        MessageStreamBlockSnapshotDTO.model_validate({"block_id": "block", **invalid})


@pytest.mark.parametrize(("field", "shape"), [
    ("session_id", {"type": "integer"}),
    ("thread_id", {"type": "object"}),
    ("turn_id", {"type": "boolean"}),
    ("turn_stream_id", {"type": "array", "items": {"type": "string"}}),
    ("snapshot_seq", {"type": "string"}),
    ("stream_status", {"type": "string", "enum": ["open", "unregistered"]}),
    ("resumable", {"type": "string"}),
    ("blocks", {"type": "object"}),
    ("interrupt_state", {"type": "string"}),
])
def test_snapshot_http_rejects_root_and_envelope_type_drift(
    http_openapi: JsonSchemaValue, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"]["MessageStreamSnapshotDTO"]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto") as captured:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(captured.value)


@pytest.mark.parametrize("field", ["session_id", "thread_id", "snapshot_seq", "interrupt_state"])
def test_snapshot_http_rejects_missing_root_fields(http_openapi: JsonSchemaValue, field: str) -> None:
    del http_openapi["components"]["schemas"]["MessageStreamSnapshotDTO"]["properties"][field]
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as captured:
        assert_public_http_schemas(http_openapi)
    assert field in str(captured.value)


@pytest.mark.parametrize("field", ["unknown_root", "interrupt_request_id", "interrupt_status"])
def test_snapshot_http_rejects_unknown_or_legacy_root_fields(http_openapi: JsonSchemaValue, field: str) -> None:
    http_openapi["components"]["schemas"]["MessageStreamSnapshotDTO"]["properties"][field] = {"type": "string"}
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致|HTTP 根不接受 wire 中断别名"):
        assert_public_http_schemas(http_openapi)


def test_snapshot_http_requires_root_export(http_openapi: JsonSchemaValue) -> None:
    del http_openapi["components"]["schemas"]["MessageStreamSnapshotDTO"]
    with pytest.raises(ValueError, match="公开 HTTP 结构未被导出: MessageStreamSnapshotDTO"):
        assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("message_name", "field_name", "mutation"), [
    ("StreamSnapshot", "new_public_field", "add"),
    ("StreamSnapshot", "interrupt_request_id", "integer"),
    ("StreamSnapshot", "interrupt_status", "remove"),
    ("MessageStreamEvent", "session_id", "integer"),
    ("MessageStreamEvent", "thread_id", "integer"),
])
def test_snapshot_root_descriptor_drift_rejects_publication(
    http_descriptor_bundle: FileDescriptorSet, tmp_path: Path, message_name: str, field_name: str, mutation: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    file = next(file for file in http_descriptor_bundle.file if file.name.endswith("/message_stream.proto"))
    message = next(message for message in file.message_type if message.name == message_name)
    if mutation == "add":
        message.field.add(name=field_name, number=100, type=FieldDescriptorProto.TYPE_STRING)
    else:
        field = next(field for field in message.field if field.name == field_name)
        if mutation == "remove":
            # optional 的合成 oneof 也一起移除，保持候选 descriptor 本身有效。
            oneof_index = field.oneof_index
            message.field.remove(field)
            del message.oneof_decl[oneof_index]
            for remaining in message.field:
                if remaining.HasField("oneof_index") and remaining.oneof_index > oneof_index:
                    remaining.oneof_index -= 1
        else:
            field.type = FieldDescriptorProto.TYPE_INT32
    candidate = tmp_path / "candidate-root.binpb"
    candidate.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto") as captured:
        openapi_export(openapi_output, candidate)
    assert field_name in str(captured.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


@pytest.mark.parametrize("invalid", [
    {"session_id": ""}, {"thread_id": ""}, {"snapshot_seq": -1}, {"current_attempt": -1},
    {"interrupt_request_id": "legacy"},
])
def test_snapshot_root_projection_keeps_pydantic_business_constraints(
    http_openapi: JsonSchemaValue, invalid: dict[str, object],
) -> None:
    assert_public_http_schemas(http_openapi)
    with pytest.raises(ValidationError):
        MessageStreamSnapshotDTO.model_validate({
            "session_id": "session", "thread_id": "thread", "turn_id": "turn", "turn_stream_id": "stream",
            "snapshot_seq": 0, "current_attempt": 0, "stream_status": "open", "agent_loop_status": "idle",
            "resumable": False, **invalid,
        })
