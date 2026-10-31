"""Gateway 控制面 HTTP 复用 descriptor 校验，结构漂移时不得发布生成物。"""

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

from app.protocol import openapi
from app.schemas.gateway.directory import GatewayDirectoryListDTO
from app.schemas.gateway.native_directories import NativeDirectoryBindRequest
from app.schemas.gateway.workspaces import (
    CreateFederationManagedWorkspaceRequest,
    CreateGatewayManagedWorkspaceRequest,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


@pytest.fixture
def gateway_openapi() -> JsonSchemaValue:
    from app.gateway.main import app

    return deepcopy(app.openapi())


def test_gateway_directory_schema_uses_actual_routes_and_keeps_business_validation(
    gateway_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(gateway_openapi)
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    components = gateway_openapi["components"]["schemas"]
    for path, method, model in (
        ("/api/gateway/local-directories", "get", "GatewayDirectoryListDTO"),
        ("/api/gateway/federation/directories", "get", "GatewayDirectoryListDTO"),
        ("/api/gateway/native-directories/workspaces/{workspace_id}", "post", "NativeDirectoryBindingDTO"),
        ("/api/gateway/native-directories/workspaces/{workspace_id}/{gateway_id}/{directory_id}", "get", "NativeDirectoryResolveDTO"),
    ):
        operation = gateway_openapi["paths"][path][method]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        envelope = components[response["$ref"].rsplit("/", 1)[1]]
        assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
        assert model in openapi.GATEWAY_HTTP_JSON_DESCRIPTORS
    binding = gateway_openapi["paths"]["/api/gateway/native-directories/workspaces/{workspace_id}"]["post"]
    assert binding["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/NativeDirectoryBindRequest")

    uri = "boxteam://gateway/gateway_" + "a" * 32 + "/resources/files/01900000000070008000000000000001"
    value = {"name": "原生目录", "path": uri, "home_path": uri, "limit": 120, "entries": [{"name": "child", "path": uri + "/child"}]}
    listing = GatewayDirectoryListDTO.model_validate(value)
    assert listing.path == uri and listing.entries[0].path == uri + "/child"
    with pytest.raises(ValidationError):
        GatewayDirectoryListDTO.model_validate({**value, "path": "/private/native-directory"})
    native = r"C:\用户\原生目录"
    assert NativeDirectoryBindRequest(native_root=native).native_root == native
    with pytest.raises(ValidationError, match="extra_forbidden"):
        NativeDirectoryBindRequest.model_validate({"native_root": native, "unexpected": True})


def test_gateway_managed_workspace_schema_uses_actual_routes_and_keeps_business_validation(
    gateway_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(gateway_openapi)
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    roots = {"GatewayManagedWorkspaceListDTO", "CreateGatewayManagedWorkspaceRequest",
             "CreateFederationManagedWorkspaceRequest"}
    assert roots <= openapi.GATEWAY_HTTP_JSON_DESCRIPTORS.keys()
    components = gateway_openapi["components"]["schemas"]
    for base, request_model in (
        ("/api/gateway/managed-workspaces", "CreateGatewayManagedWorkspaceRequest"),
        ("/api/gateway/federation/managed-workspaces", "CreateFederationManagedWorkspaceRequest"),
    ):
        for path, method in ((base, "get"), (base, "post"), (base + "/{workspace_id}", "delete")):
            operation = gateway_openapi["paths"][path][method]
            response = operation["responses"]["200"]["content"]["application/json"]["schema"]
            envelope = components[response["$ref"].rsplit("/", 1)[1]]
            assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/GatewayManagedWorkspaceListDTO")
        creation = gateway_openapi["paths"][base]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert creation["$ref"].endswith("/" + request_model)
    for model in (CreateGatewayManagedWorkspaceRequest, CreateFederationManagedWorkspaceRequest):
        for root in ("/private/native-workspace", r"C:\用户\原生工作区",
                     "boxteam://gateway/gateway_" + "a" * 32 + "/resources/files/01900000000070008000000000000001"):
            request = model(root_path=root)
            assert request.root_path == root and request.create_directory is True
        for invalid in ({"root_path": ""}, {"root_path": "/native", "name": ""},
                        {"root_path": "/native", "unexpected": True}):
            with pytest.raises(ValidationError):
                model.model_validate(invalid)


def test_gateway_ssh_schema_follows_actual_connection_routes(gateway_openapi: JsonSchemaValue) -> None:
    original = deepcopy(gateway_openapi)
    components = gateway_openapi["components"]["schemas"]
    connections = gateway_openapi["paths"]["/api/gateway/ssh-connections"]["get"]
    response = connections["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = components[response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/SshConnectionOptionListDTO")
    remote = gateway_openapi["paths"]["/api/gateway/remote-gateways"]["post"]
    assert remote["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/AddRemoteGatewayRequest")
    assert {"SshConnectionOptionListDTO", "AddRemoteGatewayRequest"} <= openapi.GATEWAY_HTTP_JSON_DESCRIPTORS.keys()
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original


@pytest.mark.parametrize("name", [
    "GatewayDirectoryListDTO", "NativeDirectoryBindRequest", "NativeDirectoryBindingDTO", "NativeDirectoryResolveDTO",
    "GatewayManagedWorkspaceListDTO", "CreateGatewayManagedWorkspaceRequest", "CreateFederationManagedWorkspaceRequest",
    "SshConnectionOptionListDTO", "AddRemoteGatewayRequest",
])
def test_gateway_schema_requires_registered_roots(gateway_openapi: JsonSchemaValue, name: str) -> None:
    del gateway_openapi["components"]["schemas"][name]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {name}"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("GatewayDirectoryListDTO", "limit", {"type": "string"}),
    ("GatewayDirectoryListDTO", "entries", {"type": "object"}),
    ("GatewayDirectoryEntryDTO", "path", {"type": "integer"}),
    ("NativeDirectoryBindRequest", "native_root", {"type": "integer"}),
    ("NativeDirectoryBindingDTO", "uri", {"type": "integer"}),
    ("NativeDirectoryResolveDTO", "native_root", {"type": "integer"}),
    ("GatewayManagedWorkspaceListDTO", "gateway_id", {"type": "integer"}),
    ("GatewayManagedWorkspaceListDTO", "items", {"type": "object"}),
    ("GatewayManagedWorkspaceDTO", "workspace_id", {"type": "integer"}),
    ("GatewayManagedWorkspaceDTO", "removable", {"type": "string"}),
    ("CreateGatewayManagedWorkspaceRequest", "root_path", {"type": "integer"}),
    ("CreateFederationManagedWorkspaceRequest", "root_path", {"type": "integer"}),
    ("SshConnectionOptionListDTO", "items", {"type": "object"}),
    ("SshConnectionOptionDTO", "host", {"type": "integer"}),
    ("SshConnectionOptionDTO", "port", {"type": "string"}),
    ("AddRemoteGatewayRequest", "private_key_path", {"type": "integer"}),
])
def test_gateway_schema_rejects_nested_public_shape_drift(
    gateway_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    gateway_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize("message_name", [
    "GatewayDirectoryListDTO", "GatewayDirectoryEntryDTO", "NativeDirectoryBindRequest",
    "NativeDirectoryBindingDTO", "NativeDirectoryResolveDTO",
    "GatewayManagedWorkspaceListDTO", "GatewayManagedWorkspaceDTO",
    "CreateGatewayManagedWorkspaceRequest", "CreateFederationManagedWorkspaceRequest",
    "SshConnectionOptionListDTO", "SshConnectionOptionDTO", "AddRemoteGatewayRequest",
])
def test_export_rejects_gateway_proto_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, message_name: str,
) -> None:
    file = next(file for file in http_descriptor_bundle.file if file.package == "boxteam.gateway.v1" and file.name.endswith("/public.proto"))
    message = next(message for message in file.message_type if message.name == message_name)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "fresh.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    output = tmp_path / "generated"
    for name in OPENAPI_SNAPSHOTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original snapshot\n")
    command = [sys.executable, "-m", "scripts.export_openapi_snapshot", "--output-root", str(output),
               "--descriptor-set", str(descriptor_set)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    (tmp_path / "export.stdout.log").write_text(result.stdout)
    (tmp_path / "export.stderr.log").write_text(result.stderr)
    (tmp_path / "export.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2) + "\n")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "公开结构与 proto 字段不一致" in result.stderr and "new_public_field" in result.stderr
    assert message_name in result.stderr
    assert all((output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)
