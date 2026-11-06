"""配置 Skill metadata 的真实路由、递归合同和离线快照保持同一结构。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic.json_schema import JsonSchemaValue

from app.protocol.generated.boxteam.workspace.v2 import skill_sources_pb2
from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from tests.contracts.api.openapi_snapshots import (
    OPENAPI_SNAPSHOTS,
    load_openapi_snapshot,
)


@pytest.fixture
def skill_descriptor_bundle(http_descriptor_bundle: FileDescriptorSet) -> FileDescriptorSet:
    descriptor = skill_sources_pb2.DESCRIPTOR
    if not any(file.name == descriptor.name for file in http_descriptor_bundle.file):
        descriptor.CopyToProto(http_descriptor_bundle.file.add())
    return http_descriptor_bundle


def test_skill_metadata_roots_follow_actual_routes(http_openapi: JsonSchemaValue) -> None:
    original = deepcopy(http_openapi)
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/resources/skill-sources"]["get"]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = http_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/SkillSourcesMetadataDTO")
    assert {"SkillSourcesMetadataDTO", "SkillSourceMetadataDTO"} <= HTTP_JSON_DESCRIPTORS.keys()
    local = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/resources/local-skills"]["get"]
    local_schema = local["responses"]["200"]["content"]["application/json"]["schema"]
    local_envelope = http_openapi["components"]["schemas"][local_schema["$ref"].rsplit("/", 1)[1]]
    assert local_envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/LocalSkillsMetadataDTO")
    assert {"LocalSkillsMetadataDTO", "LocalSkillMetadataDTO"} <= HTTP_JSON_DESCRIPTORS.keys()
    assert skill_sources_pb2.SkillSourceMetadataDTO.DESCRIPTOR.fields_by_name["priority"].type == FieldDescriptorProto.TYPE_INT64
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize("snapshot", OPENAPI_SNAPSHOTS)
def test_offline_snapshots_include_exact_metadata_and_route(http_openapi: JsonSchemaValue, snapshot: str) -> None:
    offline = cast(JsonSchemaValue, load_openapi_snapshot(snapshot))
    for model in ("SkillSourcesMetadataDTO", "SkillSourceMetadataDTO", "APIResponse_SkillSourcesMetadataDTO_",
                  "LocalSkillsMetadataDTO", "LocalSkillMetadataDTO", "APIResponse_LocalSkillsMetadataDTO_"):
        assert offline["components"]["schemas"][model] == http_openapi["components"]["schemas"][model]
    path = "/api/v1/workspaces/{workspace_id}/resources/skill-sources"
    assert offline["paths"][path] == http_openapi["paths"][path]
    local_path = "/api/v1/workspaces/{workspace_id}/resources/local-skills"
    assert offline["paths"][local_path] == http_openapi["paths"][local_path]
    assert set(offline["components"]["schemas"]["SkillSourceMetadataDTO"]["properties"]) == {
        "name", "display_uri", "available", "error_code", "priority",
    }


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("SkillSourcesMetadataDTO", "workspace_id", {"type": "integer"}),
    ("SkillSourcesMetadataDTO", "entries", {"type": "array", "items": {"type": "string"}}),
    ("SkillSourceMetadataDTO", "display_uri", {"type": "integer"}),
    ("SkillSourceMetadataDTO", "available", {"type": "string"}),
    ("SkillSourceMetadataDTO", "priority", {"type": "string"}),
    ("LocalSkillsMetadataDTO", "workspace_id", {"type": "integer"}),
    ("LocalSkillsMetadataDTO", "entries", {"type": "array", "items": {"type": "string"}}),
    ("LocalSkillMetadataDTO", "node", {"type": "string"}),
])
def test_skill_metadata_rejects_recursive_structure_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(caught.value)


@pytest.mark.parametrize("model", ["SkillSourcesMetadataDTO", "SkillSourceMetadataDTO", "LocalSkillsMetadataDTO", "LocalSkillMetadataDTO"])
def test_candidate_metadata_drift_stops_snapshot_publication(
    skill_descriptor_bundle: FileDescriptorSet, tmp_path: Path, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(message for file in skill_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="unapproved_content", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = tmp_path / "candidate.binpb"
    descriptor.write_bytes(skill_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor)
    assert "unapproved_content" in str(caught.value) and model in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)
