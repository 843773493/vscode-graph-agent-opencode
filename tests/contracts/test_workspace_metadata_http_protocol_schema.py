"""工作区信息、上下文和索引的 HTTP 结构由现有 proto 核对。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.protocol.schema import descriptor_pool_from_set
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


@pytest.mark.parametrize(("suffix", "method", "model"), [
    ("", "get", "WorkspaceDTO"),
    ("/context", "get", "WorkspaceContextDTO"),
    ("/index", "get", "WorkspaceIndexStatusDTO"),
    ("/index/rebuild", "post", "WorkspaceIndexRebuildDTO"),
])
def test_workspace_metadata_http_roots_follow_actual_routes(
    http_openapi: JsonSchemaValue, suffix: str, method: str, model: str,
) -> None:
    original = deepcopy(http_openapi)
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/workspace" + suffix][method]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = http_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
    assert model in HTTP_JSON_DESCRIPTORS
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("WorkspaceDTO", "workspace_id", {"type": "integer"}),
    ("WorkspaceDTO", "git", {"type": "array", "items": {"type": "string"}}),
    ("WorkspaceDTO", "runtime", {"type": "string"}),
    ("WorkspaceContextDTO", "languages", {"type": "array", "items": {"type": "integer"}}),
    ("WorkspaceContextDTO", "config", {"type": "string"}),
    ("WorkspaceIndexStatusDTO", "indexed_files", {"type": "string"}),
    ("WorkspaceIndexRebuildDTO", "job_id", {"type": "integer"}),
])
def test_workspace_metadata_http_rejects_structure_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(caught.value)


@pytest.mark.parametrize("model", ["WorkspaceDTO", "WorkspaceContextDTO", "WorkspaceIndexStatusDTO", "WorkspaceIndexRebuildDTO"])
def test_workspace_metadata_http_checks_candidate_descriptor(
    http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        assert_public_http_schemas(http_openapi, descriptor_pool_from_set(http_descriptor_bundle.SerializeToString()))
    assert "new_public_field" in str(caught.value)


@pytest.mark.parametrize("model", ["WorkspaceDTO", "WorkspaceIndexStatusDTO"])
def test_workspace_metadata_candidate_drift_stops_snapshot_publication(
    http_descriptor_bundle: FileDescriptorSet, tmp_path: Path, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = tmp_path / "candidate.binpb"
    descriptor.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor)
    assert "new_public_field" in str(caught.value)
    assert model in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)
