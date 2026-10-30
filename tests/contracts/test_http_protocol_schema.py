"""普通 HTTP 结构与本次编译的 proto 一致，错误时不得发布快照。"""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from jsonschema import ValidationError as SchemaValidationError
from jsonschema import validate
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol.generated.boxteam.workspace.v2 import public_pb2
from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationNodeDTO,
    ResourceNavigationRequest,
)

_SNAPSHOTS = ("src/clients/web/openapi.json", "src/clients/web/src/types/openapi/index.json")


@pytest.fixture
def http_openapi() -> JsonSchemaValue:
    from app.main import app

    return deepcopy(app.openapi())


@pytest.fixture
def http_descriptor_bundle() -> FileDescriptorSet:
    bundle = FileDescriptorSet()
    seen: set[str] = set()

    def collect(file: FileDescriptor) -> None:
        if file.name in seen:
            return
        seen.add(file.name)
        for dependency in file.dependencies:
            collect(dependency)
        file.CopyToProto(bundle.file.add())

    collect(public_pb2.DESCRIPTOR)
    return bundle


def _export(output: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.export_openapi_snapshot", "--output-root", str(output), *arguments],
        capture_output=True, text=True, timeout=30, check=False,
    )


def test_http_schema_checks_real_request_and_response_and_preserves_business_constraints(
    http_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/resources/navigation"]["post"]
    components = http_openapi["components"]
    request = {**operation["requestBody"]["content"]["application/json"]["schema"], "components": components}
    assert request["$ref"].endswith("/ResourceNavigationRequest")
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = components["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/ResourceNavigationResultDTO")
    validate({"uri": "boxteam://gateway/workspaces/workspace/files/root", "operation": "read"}, request)
    with pytest.raises(SchemaValidationError):
        validate({"uri": "resource", "operation": "read", "limit": 0}, request)
    # JSON 结构校验不代替 Pydantic 的字段关系和 URI 业务校验。
    with pytest.raises(ValidationError, match="只有 search 接受 query"):
        ResourceNavigationRequest(uri="resource", operation="read", query="needle")
    with pytest.raises(ValidationError, match="导航资源引用必须是资源 URI"):
        ResourceNavigationNodeDTO(uri="/etc/hosts", resource_shape="file", operations=("read",))


@pytest.mark.parametrize("name", HTTP_JSON_DESCRIPTORS)
def test_http_schema_requires_registered_root_in_real_openapi(http_openapi: JsonSchemaValue, name: str) -> None:
    del http_openapi["components"]["schemas"][name]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {name}"):
        assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("ResourceNavigationRequest", "operation", {"type": "integer"}),
    ("ResourceNavigationNodeDTO", "operations", {"type": "object"}),
    ("ResourceNavigationMatchDTO", "line", {"type": "string"}),
])
def test_http_schema_recursively_rejects_real_component_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


@pytest.mark.parametrize("message_name", [
    "ResourceNavigationRequest", "ResourceNavigationResultDTO", "ResourceNavigationNodeDTO", "ResourceNavigationMatchDTO",
])
def test_export_rejects_fresh_http_proto_drift_without_overwriting_snapshots(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, message_name: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type
                   if message.name == message_name)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "fresh.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    output = tmp_path / "generated"
    for name in _SNAPSHOTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original snapshot\n")
    result = _export(output, "--descriptor-set", str(descriptor_set))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "公开结构与 proto 字段不一致" in result.stderr and "new_public_field" in result.stderr
    assert all((output / name).read_bytes() == b"original snapshot\n" for name in _SNAPSHOTS)


def test_openapi_export_and_validation_only_use_isolated_output(
    tmp_path: Path, http_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet,
) -> None:
    descriptor_set = tmp_path / "fresh.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    output = tmp_path / "generated"
    result = _export(output, "--descriptor-set", str(descriptor_set), "--validate-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not output.exists()
    result = _export(output, "--descriptor-set", str(descriptor_set))
    assert result.returncode == 0, result.stdout + result.stderr
    contents = [(output / name).read_bytes() for name in _SNAPSHOTS]
    assert contents[0] == contents[1]
    assert json.loads(contents[0]) == http_openapi
