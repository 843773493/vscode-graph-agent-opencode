"""工作区导航公开结构复用 descriptor，业务约束仍由原模型核定。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from google.protobuf.descriptor_pb2 import FieldDescriptorProto, FileDescriptorSet
from pydantic import ValidationError
from pydantic.json_schema import JsonSchemaValue

from app.protocol import openapi
from app.protocol.schema import descriptor_pool_from_set
from app.schemas.gateway.search import GatewaySearchWorkspaceOwnerDTO
from app.schemas.gateway.workspaces import (
    AddLocalWorkspaceRequest,
    UpdateGatewayWorkspaceRequest,
)
from app.schemas.gateway_control import (
    WorkspaceFolderCreateRequest,
    WorkspaceNavigationNodeDTO,
    WorkspaceNavigationNodeUpdateRequest,
    WorkspaceNavigationPlacementRequest,
    WorkspaceNavigationReorderRequest,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS

NAVIGATION_ROOTS = (
    "AddLocalWorkspaceRequest", "UpdateGatewayWorkspaceRequest", "ReorderGatewayWorkspacesRequest",
    "ActivateGatewayWorkspaceResultDTO", "WorkspaceFolderCreateRequest", "WorkspaceNavigationNodeUpdateRequest",
    "WorkspaceNavigationPlacementRequest", "WorkspaceNavigationReorderRequest",
    "WorkspaceNavigationTreeDTO", "WorkspaceNavigationBreadcrumbDTO",
)


@pytest.mark.parametrize(("method", "path", "request_model", "response_model"), [
    ("post", "/api/gateway/workspaces/local", "AddLocalWorkspaceRequest", None),
    ("patch", "/api/gateway/workspaces/{workspace_id}", "UpdateGatewayWorkspaceRequest", None),
    ("put", "/api/gateway/workspaces/order", "ReorderGatewayWorkspacesRequest", None),
    ("post", "/api/gateway/workspaces/{workspace_id}/activate", None, "ActivateGatewayWorkspaceResultDTO"),
    ("get", "/api/gateway/workspace-navigation", None, "WorkspaceNavigationTreeDTO"),
    ("post", "/api/gateway/workspace-navigation/folders", "WorkspaceFolderCreateRequest", "WorkspaceNavigationTreeDTO"),
    ("patch", "/api/gateway/workspace-navigation/nodes/{node_id}", "WorkspaceNavigationNodeUpdateRequest", "WorkspaceNavigationTreeDTO"),
    ("put", "/api/gateway/workspace-navigation/order", "WorkspaceNavigationReorderRequest", "WorkspaceNavigationTreeDTO"),
    ("put", "/api/gateway/workspace-navigation/placement", "WorkspaceNavigationPlacementRequest", "WorkspaceNavigationTreeDTO"),
    ("delete", "/api/gateway/workspace-navigation/folders/{node_id}", None, "WorkspaceNavigationTreeDTO"),
    ("get", "/api/gateway/workspace-navigation/nodes/{node_id}/breadcrumb", None, "WorkspaceNavigationBreadcrumbDTO"),
])
def test_navigation_contract_follows_actual_routes(
    gateway_openapi: JsonSchemaValue, method: str, path: str, request_model: str | None, response_model: str | None,
) -> None:
    original = deepcopy(gateway_openapi)
    operation = gateway_openapi["paths"][path][method]
    if request_model is not None:
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/" + request_model)
        assert request_model in openapi.GATEWAY_HTTP_JSON_DESCRIPTORS
    if response_model is not None:
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        envelope = gateway_openapi["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
        assert envelope["properties"]["data"]["anyOf"][0]["$ref"].endswith("/" + response_model)
        assert response_model in openapi.GATEWAY_HTTP_JSON_DESCRIPTORS
    openapi.assert_gateway_http_schemas(gateway_openapi)
    assert gateway_openapi == original


@pytest.mark.parametrize("name", NAVIGATION_ROOTS)
def test_navigation_contract_requires_roots(gateway_openapi: JsonSchemaValue, name: str) -> None:
    del gateway_openapi["components"]["schemas"][name]
    with pytest.raises(ValueError, match=f"公开 HTTP 结构未被导出: {name}"):
        openapi.assert_gateway_http_schemas(gateway_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("AddLocalWorkspaceRequest", "root_path", {"type": "integer"}),
    ("UpdateGatewayWorkspaceRequest", "parent_workspace", {"type": "integer"}),
    ("ReorderGatewayWorkspacesRequest", "workspaces", {"type": "object"}),
    ("ActivateGatewayWorkspaceResultDTO", "active_workspace", {"type": "integer"}),
    ("WorkspaceFolderCreateRequest", "name", {"type": "array"}),
    ("WorkspaceNavigationNodeUpdateRequest", "parent_node_id", {"type": "integer"}),
    ("WorkspaceNavigationPlacementRequest", "target_node_id", {"type": "integer"}),
    ("WorkspaceNavigationReorderRequest", "node_ids", {"type": "array", "items": {"type": "integer"}}),
    ("WorkspaceNavigationTreeDTO", "nodes", {"type": "object"}),
    ("WorkspaceNavigationBreadcrumbDTO", "items", {"type": "object"}),
    ("WorkspaceNavigationNodeDTO", "position", {"type": "string"}),
])
def test_navigation_contract_rejects_nested_shape_drift(
    gateway_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    gateway_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as caught:
        openapi.assert_gateway_http_schemas(gateway_openapi)
    assert "/" + field in str(caught.value)


@pytest.mark.parametrize("model", (*NAVIGATION_ROOTS, "WorkspaceNavigationNodeDTO"))
def test_navigation_contract_checks_candidate_members(
    gateway_openapi: JsonSchemaValue, http_descriptor_bundle: FileDescriptorSet, model: str,
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_navigation_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool_from_set(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi.assert_gateway_http_schemas(gateway_openapi, pool)
    assert "new_navigation_field" in str(caught.value) and model in str(caught.value)


@pytest.mark.parametrize("model", ["UpdateGatewayWorkspaceRequest", "WorkspaceNavigationNodeDTO"])
def test_navigation_export_rejects_drift_before_publishing(
    tmp_path: Path, http_descriptor_bundle: FileDescriptorSet, model: str,
    openapi_output: Path, openapi_export: Callable[[Path, Path], None],
) -> None:
    message = next(message for file in http_descriptor_bundle.file for message in file.message_type if message.name == model)
    message.field.add(name="new_navigation_field", number=100, type=FieldDescriptorProto.TYPE_STRING)
    descriptor_set = tmp_path / "candidate.binpb"
    descriptor_set.write_bytes(http_descriptor_bundle.SerializeToString())
    with pytest.raises(ValueError, match="公开结构与 proto 字段不一致") as caught:
        openapi_export(openapi_output, descriptor_set)
    assert "new_navigation_field" in str(caught.value)
    assert model in str(caught.value)
    assert all((openapi_output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


def test_navigation_contract_keeps_model_business_constraints() -> None:
    assert WorkspaceFolderCreateRequest(name="  项目  ").name == "项目"
    cleared = WorkspaceNavigationNodeUpdateRequest.model_validate({"parent_node_id": None})
    assert cleared.model_fields_set == {"parent_node_id"}
    owner = GatewaySearchWorkspaceOwnerDTO(gateway_id="gateway_" + "0" * 32, workspace_id="00000000-0000-4000-8000-000000000001")
    assert WorkspaceNavigationNodeDTO(node_id="folder", name="目录", kind="workspace_folder").workspace is None
    assert WorkspaceNavigationNodeDTO(node_id="ref", name="工作区", kind="workspace_ref", workspace=owner).workspace == owner
    for mode, target in (("before", "peer"), ("after", "peer"), ("last", None)):
        value = WorkspaceNavigationPlacementRequest.model_validate({"node_id": "node", "mode": mode, "target_node_id": target})
        assert value.mode == mode and value.target_node_id == target
    for invalid in (
        {"node_id": "node", "mode": "before"}, {"node_id": "node", "mode": "after"},
        {"node_id": "node", "mode": "last", "target_node_id": "peer"},
        {"node_id": "node", "mode": "before", "target_node_id": "node"},
        {"node_id": "node", "mode": "unknown"},
    ):
        with pytest.raises(ValidationError):
            WorkspaceNavigationPlacementRequest.model_validate(invalid)
    for invalid in (
        {"node_id": "node", "name": "目录", "kind": "workspace_ref"},
        {"node_id": "node", "name": "目录", "kind": "workspace_folder", "workspace": owner.model_dump()},
        {"node_id": "node", "name": "目录", "kind": "unknown"},
    ):
        with pytest.raises(ValidationError):
            WorkspaceNavigationNodeDTO.model_validate(invalid)
    with pytest.raises(ValidationError):
        WorkspaceFolderCreateRequest(name=" ")
    with pytest.raises(ValidationError):
        WorkspaceNavigationNodeUpdateRequest.model_validate({})
    with pytest.raises(ValidationError):
        WorkspaceNavigationReorderRequest(node_ids=[])


def test_workspace_management_contract_keeps_native_input_and_explicit_null() -> None:
    for native in ("/outside/project", r"C:\outside\project"):
        assert AddLocalWorkspaceRequest(root_path=native).root_path == native
    cleared = UpdateGatewayWorkspaceRequest.model_validate({"parent_workspace": None})
    assert cleared.model_fields_set == {"parent_workspace"}
    assert UpdateGatewayWorkspaceRequest(name="  工作区  ").name == "工作区"
    for invalid in ({}, {"name": None}, {"unexpected": True}):
        with pytest.raises(ValidationError):
            UpdateGatewayWorkspaceRequest.model_validate(invalid)
