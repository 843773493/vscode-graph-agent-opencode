"""MCP 管理入口的目标、目录代际与完整响应由公开 proto 核对。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.schemas.internal_v2.mcp import McpServerListResponseDTO, McpWorkspaceTargetDTO
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS

GATEWAY_ID = "gateway_00000000000040008000000000000001"
WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(("method", "path", "model"), [
    ("get", "/servers", "McpServerListResponseDTO"),
    ("get", "/connection-operations", "McpConnectionOperationListResponseDTO"),
    ("post", "/connection-operations", "McpConnectionOperationResponseDTO"),
    ("get", "/connection-operations/{operation_id}", "McpConnectionOperationResponseDTO"),
    ("post", "/connection-operations/{operation_id}/cancel", "McpConnectionOperationResponseDTO"),
    ("get", "/connection-operations/{operation_id}/authorization", "McpAuthorizationResponseDTO"),
    ("post", "/connection-operations/{operation_id}/callback", "McpConnectionOperationResponseDTO"),
])
def test_mcp_actual_routes_publish_complete_response_roots(
    http_openapi: JsonSchemaValue, method: str, path: str, model: str,
) -> None:
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/mcp" + path][method]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response == {"$ref": "#/components/schemas/" + model}
    assert model in HTTP_JSON_DESCRIPTORS
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field"), [
    ("McpServerListResponseDTO", "catalog_revision"),
    ("McpServerListResponseDTO", "catalog_generation"),
    ("McpConnectionOperationListResponseDTO", "protocol_version"),
    ("McpConnectionOperationResponseDTO", "target"),
    ("McpAuthorizationResponseDTO", "request_id"),
    ("McpWorkspaceTargetDTO", "gateway_id"),
    ("McpWorkspaceTargetDTO", "workspace_id"),
    ("McpServerDTO", "connection_ref"),
])
def test_mcp_target_and_generation_fields_cannot_drift(
    http_openapi: JsonSchemaValue, model: str, field: str,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = {"type": "boolean"}
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(caught.value)


@pytest.mark.parametrize("model", [
    "McpServerListResponseDTO", "McpConnectionOperationListResponseDTO",
    "McpConnectionOperationResponseDTO", "McpAuthorizationResponseDTO",
    "McpWorkspaceTargetDTO", "McpServerDTO",
])
def test_mcp_candidate_descriptor_is_rejected_before_snapshot_publication(
    http_descriptor_bundle: FileDescriptorSet, tmp_path: Path, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = tmp_path / "candidate.binpb"
    descriptor.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor)
    assert model in str(caught.value) and "new_public_field" in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


def test_mcp_target_and_protocol_business_constraints() -> None:
    target = {"kind": "workspace", "gateway_id": GATEWAY_ID, "workspace_id": WORKSPACE_ID}
    payload = {"data": [], "request_id": "req-mcp", "target": target,
               "catalog_revision": "sha256:" + "a" * 64, "catalog_generation": 1}
    response = McpServerListResponseDTO.model_validate(payload)
    assert response.protocol_version == 1 and response.data == []
    assert McpWorkspaceTargetDTO.model_validate({**target, "gateway_id": None}).gateway_id is None
    for invalid in (
        {"target": {**target, "kind": "backend"}},
        {"target": {**target, "gateway_id": "gateway-other"}},
        {"target": {**target, "workspace_id": "alias"}},
        {"protocol_version": 2}, {"catalog_generation": 0}, {"catalog_revision": "unknown"},
    ):
        with pytest.raises(ValidationError):
            McpServerListResponseDTO.model_validate({**payload, **invalid})
