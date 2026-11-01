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
from app.schemas.internal_v2.message_stream import MessageStreamBlockSnapshotDTO
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
    descriptor = MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS[model]
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
    descriptor = MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS[model]
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
