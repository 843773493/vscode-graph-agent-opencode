"""Gateway 联邦工作区、访问范围和目录查询复用唯一公开结构检查。"""

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
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.gateway.federation import FederationWorkspaceDTO
from app.schemas.gateway_control import GatewaySessionSearchWorkspaceStatusDTO
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


@pytest.mark.parametrize(("path", "model"), [
    ("/api/gateway/federation/workspaces", "FederationWorkspaceListDTO"),
    ("/api/gateway/inbound-access", "GatewayInboundAccessListDTO"),
    ("/api/gateway/resources", "GatewayResourceListDTO"),
    ("/api/gateway/session-catalog/search", "GatewaySessionSearchResultsDTO"),
])
def test_catalog_schema_follows_actual_response_roots(gateway_openapi: JsonSchemaValue, path: str, model: str) -> None:
    original = deepcopy(gateway_openapi)
    response = gateway_openapi["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = gateway_openapi["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + model)
    assert model in openapi.GATEWAY_HTTP_JSON_DESCRIPTORS
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original


@pytest.mark.parametrize("name", [
    "FederationWorkspaceListDTO", "GatewayInboundAccessListDTO", "GatewayResourceListDTO", "GatewaySessionSearchResultsDTO",
])
def test_catalog_schema_requires_exported_roots(gateway_openapi: JsonSchemaValue, name: str) -> None:
    del gateway_openapi["components"]["schemas"][name]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {name}"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("FederationWorkspaceListDTO", "items", {"type": "object"}),
    ("FederationWorkspaceDTO", "backend_workspace_id", {"type": "integer"}),
    ("FederationWorkspaceDTO", "managed", {"type": "string"}),
    ("GatewayInboundAccessListDTO", "peers", {"type": "object"}),
    ("GatewayInboundPeerDTO", "credential_expires_at", {"type": "integer"}),
    ("GatewayInboundWorkspaceDTO", "system_default", {"type": "string"}),
    ("GatewayResourceListDTO", "errors", {"type": "object"}),
    ("GatewayResourceDTO", "workspace_id", {"type": "integer"}),
    ("GatewayResourceDTO", "resource", {"type": "array"}),
    ("GatewayResourceScopeErrorDTO", "message", {"type": "integer"}),
    ("GatewaySessionSearchResultsDTO", "total", {"type": "string"}),
    ("GatewaySessionSearchMatchDTO", "breadcrumb_node_ids", {"type": "object"}),
    ("GatewaySessionSearchWorkspaceStatusDTO", "error", {"type": "integer"}),
])
def test_catalog_schema_rejects_nested_shape_drift(
    gateway_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    gateway_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize("model", [
    "FederationWorkspaceListDTO", "FederationWorkspaceDTO",
    "GatewayInboundAccessListDTO", "GatewayInboundPeerDTO", "GatewayInboundWorkspaceDTO",
    "GatewayResourceListDTO", "GatewayResourceDTO", "GatewayResourceScopeErrorDTO",
    "GatewaySessionSearchResultsDTO", "GatewaySessionSearchMatchDTO", "GatewaySessionSearchWorkspaceStatusDTO",
])
def test_catalog_schema_uses_candidate_descriptor_for_all_members(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_catalog_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi.assert_gateway_http_schemas(gateway_openapi, pool)
    assert "new_catalog_field" in str(caught.value) and model in str(caught.value)


@pytest.mark.parametrize("model", ["FederationWorkspaceListDTO", "GatewayResourceScopeErrorDTO"])
def test_catalog_export_rejects_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_catalog_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
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
    assert "公开结构与 proto 字段不一致" in result.stderr and "new_catalog_field" in result.stderr
    assert model in result.stderr
    assert all((output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


def test_catalog_structure_checks_preserve_business_constraints(gateway_openapi: JsonSchemaValue) -> None:
    original = deepcopy(gateway_openapi)
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original
    workspace = {
        "workspace_id": "gateway-route", "backend_workspace_id": "01900000-0000-7000-8000-000000000001",
        "name": "工作区", "managed": True, "connection_kind": "local", "services": ["workspace_api"],
    }
    assert FederationWorkspaceDTO.model_validate(workspace).backend_workspace_id == workspace["backend_workspace_id"]
    for invalid in (
        {**workspace, "backend_workspace_id": "/private/workspace"},
        {**workspace, "connection_kind": "remote_gateway"},
        {**workspace, "services": ["unknown"]},
        {**workspace, "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            FederationWorkspaceDTO.model_validate(invalid)
    with pytest.raises(ValidationError):
        GatewaySessionSearchWorkspaceStatusDTO.model_validate({
            "workspace_id": "route", "workspace_name": "工作区", "status": "ready",
        })
