"""会话生成器结构由proto递归核对，业务规则继续由Pydantic负责。"""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationPreviewRequest,
    SessionGenerationValidationDTO,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def export_artifacts(request: pytest.FixtureRequest) -> Path:
    directory = TestRunContext.from_test_file(Path(request.node.path)).artifacts_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.mark.parametrize(("route", "request_model", "response_model"), [
    ("preview-placement", "SessionGenerationPreviewRequest", "SessionGenerationPreviewDTO"),
    ("validate-definition", "SessionGenerationValidateRequest", "SessionGenerationValidationDTO"),
])
def test_generation_http_roots_follow_actual_routes(
    http_openapi: JsonSchemaValue, route: str, request_model: str, response_model: str,
) -> None:
    original = deepcopy(http_openapi)
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/session-generations/" + route]["post"]
    request = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request["$ref"].endswith("/" + request_model)
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = http_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + response_model)
    assert {request_model, response_model} <= HTTP_JSON_DESCRIPTORS.keys()
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("SessionGenerationPreviewRequest", "session_title", {"type": "integer"}),
    ("SessionGenerationPreviewDTO", "navigation_path", {"type": "array", "items": {"type": "integer"}}),
    ("SessionGenerationValidateRequest", "path_template", {"type": "array", "items": {"type": "integer"}}),
    ("SessionGenerationValidationDTO", "reason", {"type": "integer"}),
])
def test_generation_http_rejects_request_and_response_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        assert_public_http_schemas(http_openapi)
    assert "/" + field in str(caught.value)


def test_structure_checks_keep_generation_business_rules(http_openapi: JsonSchemaValue) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    value = {"name": "  命名预览  ", "title_template": "{generator.name}"}
    assert SessionGenerationPreviewRequest.model_validate(value).name == "命名预览"
    for invalid in (
        {"name": "x" * 201},
        {"path_template": ["segment"] * 21},
        {"placement_kind": "workspace", "placement_node_id": "node"},
        {"session_strategy": "continue_existing"},
    ):
        with pytest.raises(ValidationError):
            SessionGenerationPreviewRequest.model_validate({**value, **invalid})
    valid = {"workspace_id": "workspace", "provider_available": True}
    assert SessionGenerationValidationDTO.model_validate(valid).reason is None
    for invalid in ({"provider_available": False}, {"reason": "不可用"}):
        with pytest.raises(ValidationError, match="不可用 provider 必须提供原因"):
            SessionGenerationValidationDTO.model_validate({**valid, **invalid})


@pytest.mark.parametrize("model", [
    "SessionGenerationPreviewRequest", "SessionGenerationPreviewDTO",
    "SessionGenerationValidateRequest", "SessionGenerationValidationDTO",
])
def test_generation_candidate_descriptor_rejected_before_snapshot_publication(
    http_descriptor_bundle: FileDescriptorSet, export_artifacts: Path, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor = export_artifacts / "candidate.binpb"
    descriptor.write_bytes(http_descriptor_bundle.SerializeToString())
    output = export_artifacts / "generated"
    for name in OPENAPI_SNAPSHOTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original snapshot\n")
    command = [sys.executable, "-m", "scripts.export_openapi_snapshot", "--output-root", str(output), "--descriptor-set", str(descriptor)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    (export_artifacts / "export.stdout.log").write_text(result.stdout)
    (export_artifacts / "export.stderr.log").write_text(result.stderr)
    (export_artifacts / "export.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2) + "\n")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "公开结构与 proto 字段不一致" in result.stderr and "new_public_field" in result.stderr
    assert all((output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)
