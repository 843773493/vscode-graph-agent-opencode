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

from app.core.identifier import create_prefixed_id
from app.protocol.generated.boxteam.workspace.v2 import public_pb2
from app.protocol.openapi import HTTP_JSON_DESCRIPTORS, assert_public_http_schemas
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationNodeDTO,
    ResourceNavigationRequest,
)
from app.schemas.internal_v2.workspace_resource import (
    BrowserResourceOperationRequest,
    TerminalResourceCreateRequest,
    WorkspaceResourceCommandRequest,
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


def test_file_http_routes_register_their_actual_public_json_roots(http_openapi: JsonSchemaValue) -> None:
    schemas = http_openapi["components"]["schemas"]
    roots: set[str] = set()
    for path, operations in http_openapi["paths"].items():
        if not (path.startswith("/api/v1/workspaces/{workspace_id}/workspace/files") or "file-tree-" in path):
            continue
        for operation in operations.values():
            request = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if request is not None:
                roots.add(request["schema"]["$ref"].rsplit("/", 1)[1])
            for status, response in operation["responses"].items():
                if not status.startswith("2"):
                    continue
                body = response.get("content", {}).get("application/json")
                if body is None:
                    continue
                envelope = schemas[body["schema"]["$ref"].rsplit("/", 1)[1]]
                roots.update(branch["$ref"].rsplit("/", 1)[1]
                             for branch in envelope["properties"]["data"]["anyOf"] if "$ref" in branch)
    assert {"WorkspaceFileCreateRequest", "WorkspaceFileOperationReceiptDTO", "SessionFileTreeSettingsDTO"} <= roots
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"文件入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


def test_node_debug_routes_register_their_actual_public_json_roots(http_openapi: JsonSchemaValue) -> None:
    schemas = http_openapi["components"]["schemas"]
    roots: set[str] = set()
    pending: list[JsonSchemaValue] = []
    for path, operations in http_openapi["paths"].items():
        if not path.startswith("/api/v1/workspaces/{workspace_id}/debug/node"):
            continue
        for operation in operations.values():
            request = operation.get("requestBody", {}).get("content", {}).get("application/json")
            # action+params的既有业务联合由唯一oneof投影核对。
            if request is not None and not path.endswith("/action"):
                pending.append(request["schema"])
            for status, response in operation["responses"].items():
                body = response.get("content", {}).get("application/json")
                if status.startswith("2") and body is not None:
                    pending.append(body["schema"])
    while pending:
        node = pending.pop()
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[1]
            if name.startswith("NodeDebug"):
                roots.add(name)
            else:
                pending.append(schemas[name])
        else:
            for value in node.values():
                if isinstance(value, dict):
                    pending.append(value)
                elif isinstance(value, list):
                    pending.extend(branch for branch in value if isinstance(branch, dict))
    assert {"NodeDebugCapabilitiesDTO", "NodeDebugStateDTO", "NodeDebugConfigurationDTO"} <= roots
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"调试入口缺少proto结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


def test_resource_routes_register_their_actual_requests_and_response_roots(http_openapi: JsonSchemaValue) -> None:
    prefix = "/api/v1/workspaces/{workspace_id}/resources"
    schemas = http_openapi["components"]["schemas"]
    roots: set[str] = set()
    for path, operations in http_openapi["paths"].items():
        if not (path in (prefix, prefix + "/terminals", prefix + "/browsers")
                or path.startswith(prefix + "/{resource_id}")
                or "/sessions/{session_id}/resources" in path):
            continue
        for operation in operations.values():
            request = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if request is not None:
                roots.add(request["schema"]["$ref"].rsplit("/", 1)[1])
            for status, response in operation["responses"].items():
                body = response.get("content", {}).get("application/json")
                if not status.startswith("2") or body is None:
                    continue
                envelope = schemas[body["schema"]["$ref"].rsplit("/", 1)[1]]
                roots.update(branch["$ref"].rsplit("/", 1)[1]
                             for branch in envelope["properties"]["data"]["anyOf"] if "$ref" in branch)
    assert {"WorkspaceResourceDTO", "ResourceTransportGrantDTO", "SessionResourceListDTO"} <= roots
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"资源入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("WorkspaceResourceDTO", "owner_revision", {"type": "string"}),
    ("ResourceThreadRef", "thread_id", {"type": "integer"}),
    ("ResourceTransportGrantDTO", "resource", {"type": "string"}),
    ("BrowserResourceOperationRequest", "payload", {"type": "array", "items": {}}),
    ("TerminalResourceCreateRequest", "cols", {"type": "string"}),
    ("SessionResourceDTO", "created_at", {"type": "integer"}),
    ("SessionResourceControlRequest", "params", {"type": "string"}),
])
def test_resource_http_schema_rejects_identity_revision_payload_and_nested_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_resource_structure_checks_preserve_pydantic_identity_revision_and_action_constraints(
    http_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    session_id = create_prefixed_id("ses")
    assert TerminalResourceCreateRequest(session_id=session_id, cwd_relative="/etc/hosts").cwd_relative == "/etc/hosts"
    with pytest.raises(ValidationError, match="cols"):
        TerminalResourceCreateRequest(session_id=session_id, cols=0)
    with pytest.raises(ValidationError, match="expected_revision"):
        WorkspaceResourceCommandRequest(expected_revision=0)
    with pytest.raises(ValidationError, match="thread_id"):
        WorkspaceResourceCommandRequest.model_validate({
            "expected_revision": 1, "thread": {"session_id": session_id, "thread_id": "../thread"},
        })
    with pytest.raises(ValidationError, match="action"):
        BrowserResourceOperationRequest.model_validate({"expected_revision": 1, "action": "unknown_action"})


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("NodeDebugStartRequest", "path", {"type": "integer"}),
    ("NodeDebugStateDTO", "call_stack", {"type": "object"}),
    ("NodeDebugVariableDTO", "value", {"type": "integer"}),
    ("NodeDebugSetBreakpointParams", "line", {"type": "string"}),
])
def test_node_debug_http_schema_rejects_request_state_and_action_parameter_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


@pytest.mark.parametrize("mutation", ["member", "label", "branch", "common"])
def test_node_debug_http_action_rejects_union_mapping_drift(http_openapi: JsonSchemaValue, mutation: str) -> None:
    action = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/debug/node/action"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    mapping = action["discriminator"]["mapping"]
    if mutation == "member":
        del mapping["stop"]
    elif mutation == "branch":
        action["oneOf"].pop()
    else:
        properties = http_openapi["components"]["schemas"][mapping["evaluate"].rsplit("/", 1)[1]]["properties"]
        if mutation == "label":
            properties["action"] = {"type": "string", "const": "stop"}
        else:
            del properties["thread_id"]
    with pytest.raises(ValueError, match="Node Debug HTTP 动作"):
        assert_public_http_schemas(http_openapi)


def test_file_http_schema_keeps_native_inputs_and_business_constraints(http_openapi: JsonSchemaValue) -> None:
    components = http_openapi["components"]
    schemas = components["schemas"]
    references = {**schemas["WorkspaceFileReferencesRequest"], "components": components}
    # 请求原生路径仍是合法输入；输出 URI 语义不由 proto 的基础字符串类型代替。
    paths = ["src/main.py", "/etc/hosts", r"C:\work\main.py", "boxteam://workspace/ws-1/resources/files/root/main.py"]
    validate({"paths": paths}, references)
    with pytest.raises(SchemaValidationError):
        validate({"paths": []}, references)
    with pytest.raises(SchemaValidationError):
        validate({"paths": paths * 26}, references)
    update = {**schemas["WorkspaceFileUpdateRequest"], "components": components}
    validate({"content": "/etc/hosts", "expected_revision": "a" * 64}, update)
    with pytest.raises(SchemaValidationError):
        validate({"content": "正文", "expected_revision": "invalid"}, update)
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("WorkspaceFileCreateRequest", "kind", {"type": "integer"}),
    ("WorkspaceFileMutationIntentDTO", "depends_on", {"type": "object"}),
    ("WorkspaceDirectoryKeyDTO", "workspace_id", {"type": "integer"}),
    ("WorkspaceFileOperationItemDTO", "path", {"type": "integer"}),
    ("WorkspaceFileNodeDTO", "kind", {"type": "integer"}),
    ("FileTreeShortcutDTO", "source", {"type": "integer"}),
])
def test_file_http_schema_rejects_request_receipt_snapshot_and_shortcut_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


@pytest.mark.parametrize("message_name", [
    "ResourceNavigationRequest", "ResourceNavigationResultDTO", "ResourceNavigationNodeDTO", "ResourceNavigationMatchDTO",
    "WorkspaceFileCreateRequest", "WorkspaceDirectoryKeyDTO", "WorkspaceFileMutationIntentDTO",
    "WorkspaceFileOperationReceiptDTO", "WorkspaceFileOperationItemDTO", "WorkspaceFileNodeDTO", "FileTreeShortcutDTO",
    "NodeDebugStateDTO", "NodeDebugConfigurationDTO", "NodeDebugVariableDTO", "NodeDebugSetBreakpointParams",
    "WorkspaceResourceDTO", "ResourceThreadRef", "TerminalResourceCreateRequest",
    "BrowserResourceOperationRequest", "SessionResourceControlResultDTO",
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
