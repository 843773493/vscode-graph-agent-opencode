"""运行时公开响应由proto核对，生命周期与阻塞资源规则仍归业务模型。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.internal_v2.runtime import RuntimeDrainResultDTO, RuntimeStatusDTO


@pytest.mark.parametrize(("route", "method", "model"), [
    ("status", "get", "RuntimeStatusDTO"),
    ("drain", "post", "RuntimeDrainResultDTO"),
    ("drain/cancel", "post", "RuntimeDrainResultDTO"),
    ("drain/force", "post", "RuntimeDrainResultDTO"),
    ("log-snapshot", "post", "UiSnapshotResultDTO"),
])
def test_runtime_http_roots_follow_actual_routes(
    http_openapi: JsonSchemaValue, route: str, method: str, model: str,
) -> None:
    original = deepcopy(http_openapi)
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/runtime/" + route][method]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = http_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
    assert model in HTTP_JSON_DESCRIPTORS
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("RuntimeStatusDTO", "pid", {"type": "string"}),
    ("RuntimeDrainResultDTO", "accepting_jobs", {"type": "integer"}),
    ("RuntimeDrainBlockerDTO", "detail", {"type": "integer"}),
    ("UiSnapshotResultDTO", "html_url", {"type": "integer"}),
])
def test_runtime_http_rejects_response_and_nested_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as captured:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(captured.value)


@pytest.mark.parametrize("model", [
    "RuntimeStatusDTO", "RuntimeDrainResultDTO", "RuntimeDrainBlockerDTO", "UiSnapshotResultDTO",
])
def test_runtime_http_uses_candidate_descriptor_for_nested_members(
    http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as captured:
        assert_public_http_schemas(http_openapi, pool)
    assert "new_public_field" in str(captured.value)


def test_runtime_structure_checks_preserve_business_constraints(http_openapi: JsonSchemaValue) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    status = {
        "pid": 1, "uptime_seconds": 0, "workspace_id": "workspace", "active_jobs": 0,
        "lifecycle_state": "ready", "accepting_jobs": True,
    }
    assert RuntimeStatusDTO.model_validate(status).blockers == []
    drain = {"lifecycle_state": "draining", "accepting_jobs": False}
    assert RuntimeDrainResultDTO.model_validate(drain).interrupted_resources == 0
    for invalid in ("unknown", "READY"):
        with pytest.raises(ValidationError):
            RuntimeStatusDTO.model_validate({**status, "lifecycle_state": invalid})
    with pytest.raises(ValidationError):
        RuntimeDrainResultDTO.model_validate({**drain, "blockers": [{
            "kind": "unknown", "resource_id": "resource", "session_id": "session", "status": "running",
        }]})
