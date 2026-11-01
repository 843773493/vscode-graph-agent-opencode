"""Gateway 控制面 HTTP 复用 descriptor 校验，结构漂移时不得发布生成物。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol import openapi
from app.schemas.gateway.diagnostics import (
    DevelopmentRuntimeRestartDTO,
    GatewayDiagnosticLogDTO,
    GatewayHealthDTO,
)
from app.schemas.gateway.directory import GatewayDirectoryListDTO
from app.schemas.gateway.lifecycle import GatewayRuntimeBlockerDTO
from app.schemas.gateway.native_directories import NativeDirectoryBindRequest
from app.schemas.gateway.port_forwarding import (
    ChangePortForwardLabelRequest,
    ChangePortForwardLocalPortRequest,
    CreatePortForwardRequest,
)
from app.schemas.gateway.users import (
    AcquireGatewayUserRequest,
    CreateGatewayGuestRequest,
    CreateGatewayUserRequest,
    GatewayUserAccessDTO,
)
from app.schemas.gateway.workspaces import (
    CreateFederationManagedWorkspaceRequest,
    CreateGatewayManagedWorkspaceRequest,
    GatewayServiceStatusDTO,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


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


def test_gateway_port_forward_schema_follows_control_routes_and_keeps_business_rules(gateway_openapi: JsonSchemaValue) -> None:
    original = deepcopy(gateway_openapi)
    components = gateway_openapi["components"]["schemas"]
    base = "/api/gateway/workspaces/{workspace_id}/port-forwards"
    for suffix, method in (("", "get"), ("", "post"), ("/{forward_id}", "delete"),
                           ("/{forward_id}/reconnect", "post"), ("/{forward_id}/local-port", "patch"),
                           ("/{forward_id}/label", "patch")):
        operation = gateway_openapi["paths"][base + suffix][method]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        envelope = components[response["$ref"].rsplit("/", 1)[1]]
        assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/PortForwardListDTO")
    for suffix, method, model in (("", "post", "CreatePortForwardRequest"),
                                  ("/{forward_id}/local-port", "patch", "ChangePortForwardLocalPortRequest"),
                                  ("/{forward_id}/label", "patch", "ChangePortForwardLabelRequest")):
        body = gateway_openapi["paths"][base + suffix][method]["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith("/" + model)
    assert {"PortForwardListDTO", "CreatePortForwardRequest", "ChangePortForwardLocalPortRequest",
            "ChangePortForwardLabelRequest"} <= openapi.GATEWAY_HTTP_JSON_DESCRIPTORS.keys()
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    request = CreatePortForwardRequest(remote_port=1, label="  预览  ")
    assert request.protocol == "http" and request.local_port is None and request.label == "预览"
    assert ChangePortForwardLabelRequest().label is None
    for model, invalid in (
        (CreatePortForwardRequest, {"remote_port": 0}),
        (CreatePortForwardRequest, {"remote_port": 80, "protocol": "file"}),
        (CreatePortForwardRequest, {"remote_port": 80, "unexpected": True}),
        (ChangePortForwardLocalPortRequest, {"local_port": 65536}),
        (ChangePortForwardLabelRequest, {"label": "x" * 121}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(invalid)


def test_gateway_user_access_schema_follows_control_routes_and_keeps_business_rules(gateway_openapi: JsonSchemaValue) -> None:
    original = deepcopy(gateway_openapi)
    components = gateway_openapi["components"]["schemas"]
    for suffix, method, model in (
        ("", "get", "GatewayUserListDTO"), ("", "post", "GatewayUserDTO"),
        ("/{user_id}/access", "post", "GatewayUserAccessDTO"),
        ("/{user_id}/takeover", "post", "GatewayUserAccessDTO"),
        ("/guest", "post", "GatewayUserAccessDTO"),
        ("/current", "get", "GatewayUserAccessDTO"),
        ("/current/heartbeat", "post", "GatewayUserAccessDTO"),
    ):
        operation = gateway_openapi["paths"]["/api/gateway/users" + suffix][method]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        envelope = components[response["$ref"].rsplit("/", 1)[1]]
        assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
    for suffix, model in (("", "CreateGatewayUserRequest"), ("/{user_id}/access", "AcquireGatewayUserRequest"),
                          ("/{user_id}/takeover", "AcquireGatewayUserRequest"), ("/guest", "CreateGatewayGuestRequest")):
        body = gateway_openapi["paths"]["/api/gateway/users" + suffix]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith("/" + model)
    assert {"GatewayUserListDTO", "GatewayUserDTO", "GatewayUserAccessDTO", "CreateGatewayUserRequest",
            "AcquireGatewayUserRequest", "CreateGatewayGuestRequest"} <= openapi.GATEWAY_HTTP_JSON_DESCRIPTORS.keys()
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    assert CreateGatewayUserRequest(display_name="  用户  ").display_name == "用户"
    assert AcquireGatewayUserRequest(client_label="  本机  ").client_label == "本机"
    assert CreateGatewayGuestRequest(tracking={"source": "local", "attempt": 1, "active": True}).tracking == {
        "source": "local", "attempt": 1, "active": True,
    }
    for model, invalid in (
        (CreateGatewayUserRequest, {"display_name": "  "}),
        (CreateGatewayUserRequest, {"display_name": "用户", "user_id": "x" * 65}),
        (AcquireGatewayUserRequest, {"client_label": "x" * 121}),
        (AcquireGatewayUserRequest, {"unexpected": True}),
        (CreateGatewayGuestRequest, {"tracking": {"source": []}}),
        (GatewayUserAccessDTO, {"kind": "invented", "lease_generation": 1}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(invalid)


def test_gateway_diagnostic_schema_follows_management_routes_and_keeps_business_rules(gateway_openapi: JsonSchemaValue) -> None:
    original = deepcopy(gateway_openapi)
    components = gateway_openapi["components"]["schemas"]
    for path, method, model in (
        ("/api/gateway/health", "get", "GatewayHealthDTO"),
        ("/api/gateway/diagnostics", "get", "GatewayDiagnosticsDTO"),
        ("/api/gateway/federation/diagnostics", "get", "GatewayDiagnosticsDTO"),
        ("/api/gateway/runtime/restart-development", "post", "DevelopmentRuntimeRestartDTO"),
    ):
        operation = gateway_openapi["paths"][path][method]
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        envelope = components[response["$ref"].rsplit("/", 1)[1]]
        assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
        assert model in openapi.GATEWAY_HTTP_JSON_DESCRIPTORS
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    assert GatewayHealthDTO(gateway_id="gateway_test", process_id=1).status == "ok"
    restart = DevelopmentRuntimeRestartDTO(previous_process_id=1, helper_process_id=2, delay_ms=0)
    assert restart.status == "scheduled"
    raw_log = "本地异常 /private/source.py; C:\\用户\\source.py; \\\\server\\share\\source.py"
    log = GatewayDiagnosticLogDTO(
        log_id="gateway:stderr", source="gateway", service="gateway", label="原始日志",
        status="available", tail=raw_log,
    )
    assert log.model_dump(mode="json")["tail"] == raw_log
    for model, invalid in (
        (GatewayHealthDTO, {"gateway_id": "gateway_test", "process_id": 0}),
        (GatewayHealthDTO, {"gateway_id": "gateway_test", "process_id": 1, "status": "ready"}),
        (DevelopmentRuntimeRestartDTO, {**restart.model_dump(), "helper_process_id": 0}),
        (DevelopmentRuntimeRestartDTO, {**restart.model_dump(), "delay_ms": -1}),
        (DevelopmentRuntimeRestartDTO, {**restart.model_dump(), "status": "running"}),
        (GatewayDiagnosticLogDTO, {**log.model_dump(), "line_count": -1}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(invalid)


def test_gateway_service_status_follows_workspace_route_and_keeps_business_validation(
    gateway_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(gateway_openapi)
    response = gateway_openapi["paths"]["/api/gateway/workspaces"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    schemas = gateway_openapi["components"]["schemas"]
    envelope = schemas[response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/GatewayWorkspaceListDTO")
    assert schemas["GatewayWorkspaceListDTO"]["properties"]["items"]["items"]["$ref"].endswith("/GatewayWorkspaceDTO")
    services = schemas["GatewayWorkspaceDTO"]["properties"]["services"]
    assert services["additionalProperties"]["$ref"].endswith("/GatewayServiceStatusDTO")
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    service = GatewayServiceStatusDTO(status="ready", health_path="/health", local_port=8010)
    assert service.model_dump()["local_port"] == 8010
    with pytest.raises(ValidationError, match="status"):
        GatewayServiceStatusDTO.model_validate({**service.model_dump(), "status": "unknown"})


def test_gateway_runtime_blocker_follows_lifecycle_routes_and_keeps_business_validation(
    gateway_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(gateway_openapi)
    schemas = gateway_openapi["components"]["schemas"]
    for action, model in (("start", "GatewayRuntimeStateResultDTO"), ("stop", "GatewayRuntimeStateResultDTO"),
                          ("restart-safe", "GatewayRuntimeRestartResultDTO"), ("restart-force", "GatewayRuntimeRestartResultDTO")):
        response = gateway_openapi["paths"][f"/api/gateway/workspaces/{{workspace_id}}/runtime/{action}"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        envelope = schemas[response["$ref"].rsplit("/", 1)[1]]
        assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
        assert schemas[model]["properties"]["blockers"]["items"]["$ref"].endswith("/GatewayRuntimeBlockerDTO")
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    blocker = GatewayRuntimeBlockerDTO(kind="job", workspace_id="workspace-a", resource_id="job-a",
                                       session_id="session-a", status="running")
    assert blocker.detail is None
    with pytest.raises(ValidationError, match="kind"):
        GatewayRuntimeBlockerDTO.model_validate({**blocker.model_dump(), "kind": "unknown"})


@pytest.mark.parametrize("name", [
    "GatewayDirectoryListDTO", "NativeDirectoryBindRequest", "NativeDirectoryBindingDTO", "NativeDirectoryResolveDTO",
    "GatewayManagedWorkspaceListDTO", "CreateGatewayManagedWorkspaceRequest", "CreateFederationManagedWorkspaceRequest",
    "SshConnectionOptionListDTO", "AddRemoteGatewayRequest",
    "PortForwardListDTO", "CreatePortForwardRequest", "ChangePortForwardLocalPortRequest", "ChangePortForwardLabelRequest",
    "GatewayUserListDTO", "GatewayUserDTO", "GatewayUserAccessDTO", "CreateGatewayUserRequest",
    "AcquireGatewayUserRequest", "CreateGatewayGuestRequest",
    "GatewayHealthDTO", "GatewayDiagnosticsDTO", "DevelopmentRuntimeRestartDTO",
    "GatewayServiceStatusDTO", "GatewayRuntimeBlockerDTO",
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
    ("PortForwardListDTO", "items", {"type": "object"}),
    ("PortForwardDTO", "remote_port", {"type": "string"}),
    ("PortForwardDTO", "local_host", {"type": "integer"}),
    ("CreatePortForwardRequest", "remote_port", {"type": "string"}),
    ("ChangePortForwardLocalPortRequest", "local_port", {"type": "string"}),
    ("ChangePortForwardLabelRequest", "label", {"type": "integer"}),
    ("GatewayUserListDTO", "items", {"type": "object"}),
    ("GatewayUserDTO", "user_id", {"type": "integer"}),
    ("GatewayUserLeaseDTO", "occupied", {"type": "string"}),
    ("GatewayUserAccessDTO", "lease_generation", {"type": "string"}),
    ("CreateGatewayUserRequest", "display_name", {"type": "integer"}),
    ("AcquireGatewayUserRequest", "client_label", {"type": "integer"}),
    ("CreateGatewayGuestRequest", "tracking", {"type": "array"}),
    ("GatewayHealthDTO", "process_id", {"type": "string"}),
    ("DevelopmentRuntimeRestartDTO", "delay_ms", {"type": "string"}),
    ("GatewayDiagnosticsDTO", "logs", {"type": "object"}),
    ("GatewayDiagnosticsDTO", "status", {"type": "integer"}),
    ("GatewayDiagnosticLogDTO", "line_count", {"type": "string"}),
    ("GatewayDiagnosticWorkspaceDTO", "managed", {"type": "string"}),
    ("GatewayDiagnosticProcessDTO", "workspace_ids", {"type": "object"}),
    ("GatewayDiagnosticLoggingStateDTO", "pid", {"type": "string"}),
    ("GatewayDiagnosticLogSinkDTO", "inode", {"type": "integer"}),
    ("GatewayServiceStatusDTO", "status", {"type": "integer"}),
    ("GatewayServiceStatusDTO", "health_path", {"type": "integer"}),
    ("GatewayServiceStatusDTO", "local_port", {"type": "string"}),
    ("GatewayServiceStatusDTO", "remote_port", {"type": "string"}),
    ("GatewayServiceStatusDTO", "error", {"type": "array", "items": {"type": "string"}}),
    ("GatewayRuntimeBlockerDTO", "kind", {"type": "integer"}),
    ("GatewayRuntimeBlockerDTO", "workspace_id", {"type": "integer"}),
    ("GatewayRuntimeBlockerDTO", "resource_id", {"type": "integer"}),
    ("GatewayRuntimeBlockerDTO", "session_id", {"type": "integer"}),
    ("GatewayRuntimeBlockerDTO", "status", {"type": "integer"}),
    ("GatewayRuntimeBlockerDTO", "detail", {"type": "array", "items": {"type": "string"}}),
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
    "PortForwardListDTO", "PortForwardDTO", "CreatePortForwardRequest",
    "ChangePortForwardLocalPortRequest", "ChangePortForwardLabelRequest",
    "GatewayUserListDTO", "GatewayUserDTO", "GatewayUserLeaseDTO", "GatewayUserAccessDTO",
    "CreateGatewayUserRequest", "AcquireGatewayUserRequest", "CreateGatewayGuestRequest",
    "GatewayHealthDTO", "GatewayDiagnosticsDTO", "DevelopmentRuntimeRestartDTO",
    "GatewayDiagnosticLogDTO", "GatewayDiagnosticWorkspaceDTO", "GatewayDiagnosticProcessDTO",
    "GatewayDiagnosticLoggingStateDTO", "GatewayDiagnosticLogSinkDTO",
    "GatewayServiceStatusDTO", "GatewayRuntimeBlockerDTO",
])
def test_export_rejects_gateway_proto_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, message_name: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    file = next(file for file in http_descriptor_bundle.file if file.package == "boxteam.gateway.v1" and file.name.endswith("/public.proto"))
    message = next(message for message in file.message_type if message.name == message_name)
    message.field.add(name="new_public_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "fresh.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor_set)
    assert "new_public_field" in str(caught.value)
    assert message_name in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)
