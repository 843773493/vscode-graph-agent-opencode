"""运行组 HTTP 请求保留数组外形，包装消息由具名投影核对。"""

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

from app.protocol.openapi import assert_gateway_http_schemas
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.gateway.lifecycle import GatewayRuntimeGroupRequest
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


@pytest.mark.parametrize(("method", "route"), [
    ("delete", "/api/gateway/federation/managed-workspaces/{workspace_id}"),
    ("delete", "/api/gateway/managed-workspaces/{workspace_id}"),
    ("delete", "/api/gateway/workspaces/{workspace_id}"),
    ("post", "/api/gateway/workspaces/{workspace_id}/runtime/start"),
    ("post", "/api/gateway/workspaces/{workspace_id}/runtime/stop"),
    ("post", "/api/gateway/workspaces/{workspace_id}/runtime/restart-safe"),
    ("post", "/api/gateway/workspaces/{workspace_id}/runtime/restart-force"),
])
def test_runtime_group_contract_follows_actual_routes(
    gateway_openapi: JsonSchemaValue, method: str, route: str,
) -> None:
    original = deepcopy(gateway_openapi)
    operation = gateway_openapi["paths"][route][method]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["anyOf"] == [
        {"$ref": "#/components/schemas/GatewayRuntimeGroupRequest"}, {"type": "null"},
    ]
    assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original


def test_runtime_group_contract_requires_root(gateway_openapi: JsonSchemaValue) -> None:
    del gateway_openapi["components"]["schemas"]["GatewayRuntimeGroupRequest"]
    with pytest.raises(ValueError, match="公开 HTTP 结构未被导出: GatewayRuntimeGroupRequest"):
        assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize("field", [
    "expected_backend_generation", "expected_workspace_ids", "desired_workspace_ids", "unexpected",
])
def test_runtime_group_contract_rejects_field_drift(gateway_openapi: JsonSchemaValue, field: str) -> None:
    properties = gateway_openapi["components"]["schemas"]["GatewayRuntimeGroupRequest"]["properties"]
    if field == "unexpected":
        properties[field] = {"type": "string"}
    else:
        del properties[field]
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        assert_gateway_http_schemas(gateway_openapi)
    assert field in str(caught.value)


@pytest.mark.parametrize(("field", "shape"), [
    ("expected_backend_generation", {"type": "integer"}),
    ("expected_workspace_ids", {"type": "array", "items": {"type": "integer"}}),
    ("desired_workspace_ids", {"type": "object"}),
])
def test_runtime_group_contract_rejects_shape_drift(
    gateway_openapi: JsonSchemaValue, field: str, shape: JsonSchemaValue,
) -> None:
    gateway_openapi["components"]["schemas"]["GatewayRuntimeGroupRequest"]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        assert_gateway_http_schemas(gateway_openapi)
    assert "/" + field in str(caught.value)


@pytest.mark.parametrize("model", ["GatewayRuntimeGroupRequest", "GatewayRuntimeDesiredWorkspaceSet"])
def test_runtime_group_contract_checks_candidate_members(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_group_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        assert_gateway_http_schemas(gateway_openapi, pool)
    assert "new_group_field" in str(caught.value) and model in str(caught.value)


@pytest.mark.parametrize("change", ["wrapper_name", "wrapper_item_type", "group_field_type"])
def test_runtime_group_contract_checks_candidate_wrapper_shape(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, change: str,
) -> None:
    messages = {message.name: message for file in http_descriptor_bundle.file for message in file.message_type}
    if change == "group_field_type":
        field = next(field for field in messages["GatewayRuntimeGroupRequest"].field if field.name == "desired_workspace_ids")
        field.type = FieldDescriptorProto.TYPE_STRING
        field.ClearField("type_name")
    else:
        field = messages["GatewayRuntimeDesiredWorkspaceSet"].field[0]
        if change == "wrapper_name":
            field.name = "replacement_ids"
        else:
            field.type = FieldDescriptorProto.TYPE_INT64
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto (字段|类型)不一致"):
        assert_gateway_http_schemas(gateway_openapi, pool)


def test_runtime_group_export_rejects_wrapper_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type
                   if message.name == "GatewayRuntimeDesiredWorkspaceSet")
    message.field.add(name="new_group_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "candidate.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    output = tmp_path / "generated"
    for name in OPENAPI_SNAPSHOTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original snapshot\n")
    command = [sys.executable, "-m", "scripts.export_openapi_snapshot", "--output-root", str(output),
               "--descriptor-set", str(descriptor_set)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "export.stdout.log").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "export.stderr.log").write_text(result.stderr, encoding="utf-8")
    (tmp_path / "export.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2) + "\n")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "公开结构与 proto 字段不一致" in result.stderr and "new_group_field" in result.stderr
    assert "GatewayRuntimeDesiredWorkspaceSet" in result.stderr
    assert all((output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


def test_runtime_group_contract_keeps_native_http_semantics() -> None:
    preconditions = {"expected_backend_generation": "generation", "expected_workspace_ids": ["member-a", "member-b"]}
    assert GatewayRuntimeGroupRequest.model_validate({}).desired_workspace_ids is None
    assert GatewayRuntimeGroupRequest.model_validate({**preconditions, "desired_workspace_ids": None}).desired_workspace_ids is None
    assert GatewayRuntimeGroupRequest.model_validate({**preconditions, "desired_workspace_ids": []}).model_dump(mode="json") == {
        **preconditions, "desired_workspace_ids": [],
    }
    assert GatewayRuntimeGroupRequest.model_validate({**preconditions, "desired_workspace_ids": ["member-b"]}).desired_workspace_ids == ["member-b"]
    for invalid in (
        {"expected_backend_generation": "generation"}, {"expected_workspace_ids": ["member-a"]},
        {"desired_workspace_ids": []}, {**preconditions, "expected_workspace_ids": []},
        {**preconditions, "expected_backend_generation": ""},
        {**preconditions, "expected_workspace_ids": ["member-a", "member-a"]},
        {**preconditions, "desired_workspace_ids": ["member-a", "member-a"]},
        {**preconditions, "desired_workspace_ids": {"workspace_ids": []}},
        {**preconditions, "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            GatewayRuntimeGroupRequest.model_validate(invalid)
