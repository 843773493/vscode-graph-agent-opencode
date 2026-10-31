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
from app.schemas.internal_v2.goal import SessionGoalSetRequest
from app.schemas.internal_v2.job import JobControlRequest, JobDispatchSnapshotDTO
from app.schemas.internal_v2.mcp import (
    McpAuthorizationCallbackDTO,
    McpConnectionRequestDTO,
)
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationNodeDTO,
    ResourceNavigationRequest,
)
from app.schemas.internal_v2.session import SessionDTO, SessionInformationSessionDTO
from app.schemas.internal_v2.tool import ToolSelectionPatchRequest
from app.schemas.internal_v2.tool_test import ToolTestStartRequest
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


def _public_roots(document: JsonSchemaValue, pending: list[JsonSchemaValue]) -> set[str]:
    """穿过 HTTP 信封、分页与数组，具名 proto 根的成员交给正式检查递归处理。"""
    schemas = document["components"]["schemas"]
    roots: set[str] = set()
    visited: set[str] = set()
    while pending:
        node = pending.pop()
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[1]
            if name in public_pb2.DESCRIPTOR.message_types_by_name:
                roots.add(name)
            elif name not in visited:
                visited.add(name)
                pending.append(schemas[name])
        else:
            for value in node.values():
                if isinstance(value, dict):
                    pending.append(value)
                elif isinstance(value, list):
                    pending.extend(branch for branch in value if isinstance(branch, dict))
    return roots


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
    roots = _public_roots(http_openapi, pending)
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


def test_execution_http_routes_check_actual_job_and_goal_roots(http_openapi: JsonSchemaValue) -> None:
    schemas = http_openapi["components"]["schemas"]
    pending: list[JsonSchemaValue] = []
    prefix = "/api/v1/workspaces/{workspace_id}"
    for path in ("/jobs/{job_id}", "/jobs/{job_id}/steps",
                 "/jobs/{job_id}/control", "/sessions/{session_id}/goal"):
        for operation in http_openapi["paths"][prefix + path].values():
            request = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if request is not None:
                pending.append(request["schema"])
            for status, response in operation["responses"].items():
                body = response.get("content", {}).get("application/json")
                if status.startswith("2") and body is not None:
                    pending.append(body["schema"])
    # 派发快照由真实消息接受回执嵌套暴露，并非独立 HTTP 回包。
    pending.append(schemas["MessageRunAccepted"]["properties"]["dispatch"])
    roots = _public_roots(http_openapi, pending)
    assert roots == {"JobDTO", "StepDTO", "JobControlRequest", "JobControlResponseDTO", "JobDispatchSnapshotDTO",
                     "SessionGoalDTO", "SessionGoalSetRequest", "SessionGoalClearResultDTO"}
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"执行入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("JobDTO", "thread_id", {"type": "integer"}),
    ("JobDTO", "created_at", {"type": "integer"}),
    ("StepDTO", "input_payload", {"type": "array", "items": {}}),
    ("JobControlRequest", "params", {"type": "array", "items": {}}),
    ("JobControlResponseDTO", "session_id", {"type": "integer"}),
    ("JobDispatchSnapshotDTO", "queued_job_count", {"type": "string"}),
    ("SessionGoalDTO", "token_budget", {"type": "string"}),
    ("SessionGoalClearResultDTO", "cleared", {"type": "string"}),
])
def test_execution_http_schema_rejects_identity_time_control_queue_and_budget_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_execution_structure_checks_preserve_pydantic_control_queue_and_goal_rules(
    http_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    with pytest.raises(ValidationError, match="agent scope requires agent_id"):
        JobControlRequest.model_validate({"scope": "agent", "action": "cancel"})
    with pytest.raises(ValidationError, match="instruction action requires message"):
        JobControlRequest.model_validate({"action": "replace_instruction"})
    with pytest.raises(ValidationError, match="queued_job_count"):
        JobDispatchSnapshotDTO.model_validate({
            "session_id": "session", "thread_id": "thread", "job_id": "job", "job_status": "queued",
            "queued_jobs_ahead": 0, "queued_job_count": -1, "pending_job_count": 0,
        })
    with pytest.raises(ValidationError, match="token_budget"):
        SessionGoalSetRequest(token_budget=0)
    with pytest.raises(ValidationError, match="objective"):
        SessionGoalSetRequest(objective="")
    assert SessionGoalSetRequest(token_budget=None).token_budget is None


def test_session_http_routes_check_create_update_list_detail_and_information_roots(
    http_openapi: JsonSchemaValue,
) -> None:
    prefix = "/api/v1/workspaces/{workspace_id}/sessions"
    pending: list[JsonSchemaValue] = []
    for path, methods in ((prefix, ("post", "get")), (prefix + "/{session_id}", ("get", "patch")),
                          (prefix + "/{session_id}/information", ("get",))):
        for method in methods:
            operation = http_openapi["paths"][path][method]
            request = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if request is not None:
                pending.append(request["schema"])
            pending.append(operation["responses"]["200"]["content"]["application/json"]["schema"])
    roots = _public_roots(http_openapi, pending)
    assert roots == {"SessionCreateRequest", "SessionUpdateRequest", "SessionDTO", "SessionInformationSnapshotDTO"}
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"会话入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("SessionCreateRequest", "folder_id", {"type": "integer"}),
    ("SessionUpdateRequest", "provider_id", {"type": "integer"}),
    ("SessionDTO", "created_at", {"type": "integer"}),
    ("SessionGenerationOriginDTO", "generator_id", {"type": "integer"}),
    ("SessionInformationResourceSummaryDTO", "active", {"type": "object"}),
    ("SessionInformationResourceDTO", "updated_at", {"type": "integer"}),
    ("SessionInformationRelationsDTO", "child_ids", {"type": "object"}),
    ("SessionInformationErrorDTO", "timestamp", {"type": "integer"}),
])
def test_session_http_schema_rejects_request_identity_time_origin_and_nested_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_session_structure_checks_keep_pydantic_uri_origin_and_length_rules(http_openapi: JsonSchemaValue) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    session = {
        "session_id": create_prefixed_id("ses"), "workspace_id": "workspace", "resource_identity": "session:identity",
        "uri": "boxteam://workspace/workspace/resources/session/session", "scope": "workspace", "title": "会话",
        "current_agent_id": "agent", "created_at": "2026-10-31T00:00:00Z", "updated_at": "2026-10-31T00:00:00Z",
    }
    assert SessionDTO.model_validate(session).uri == session["uri"]
    with pytest.raises(ValidationError, match="context_fork 会话缺少 context_source_session_id"):
        SessionDTO.model_validate({**session, "kind": "context_fork"})
    with pytest.raises(ValidationError, match="URI"):
        SessionDTO.model_validate({**session, "uri": "/etc/hosts"})
    with pytest.raises(ValidationError, match="title"):
        SessionInformationSessionDTO.model_validate({**session, "title": "长" * 513, "kind": "normal"})


def test_tool_http_routes_check_catalog_selection_test_and_connection_management_roots(
    http_openapi: JsonSchemaValue,
) -> None:
    pending: list[JsonSchemaValue] = []
    prefix = "/api/v1/workspaces/{workspace_id}"
    for path, operations in http_openapi["paths"].items():
        if not (path == prefix + "/tools" or path.startswith((prefix + "/tools/", prefix + "/mcp/"))):
            continue
        for operation in operations.values():
            request = operation.get("requestBody", {}).get("content", {}).get("application/json")
            if request is not None:
                pending.append(request["schema"])
            for status, response in operation["responses"].items():
                body = response.get("content", {}).get("application/json")
                if status.startswith("2") and body is not None:
                    pending.append(body["schema"])
    roots = _public_roots(http_openapi, pending)
    assert roots == {
        "McpServerDTO", "McpConnectionOperationDTO", "McpConnectionRequestDTO",
        "McpAuthorizationDTO", "McpAuthorizationCallbackDTO", "ToolDTO", "ToolSelectionPatchRequest",
        "ToolTestStartRequest", "ToolTestRunDTO", "ToolTestRunListDTO",
    }
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"工具入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("McpServerDTO", "enabled", {"type": "string"}),
    ("McpToolDTO", "remote_name", {"type": "integer"}),
    ("McpConnectionOperationDTO", "deadline", {"type": "string"}),
    ("McpConnectionRequestDTO", "action", {"type": "integer"}),
    ("McpAuthorizationDTO", "authorization_url", {"type": "integer"}),
    ("McpAuthorizationCallbackDTO", "state", {"type": "integer"}),
    ("ToolDTO", "parameters", {"type": "array", "items": {}}),
    ("ToolSelectionChange", "model_visible", {"type": "string"}),
    ("ToolTestStartRequest", "provider_ids", {"type": "object"}),
    ("ToolTestRunDTO", "created_at", {"type": "integer"}),
    ("ToolTestAttemptDTO", "execution_succeeded", {"type": "integer"}),
    ("ToolTestProviderResultDTO", "success_rate", {"type": "string"}),
])
def test_tool_http_schema_rejects_catalog_selection_authorization_and_nested_test_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_tool_structure_checks_preserve_pydantic_operation_secret_and_selection_rules(
    http_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    with pytest.raises(ValidationError, match="operation_id"):
        McpConnectionRequestDTO(operation_id="../operation", server_id="server", action="connect")
    with pytest.raises(ValidationError, match="action"):
        McpConnectionRequestDTO.model_validate({"operation_id": "operation", "server_id": "server", "action": "hold"})
    with pytest.raises(ValidationError) as error:
        McpAuthorizationCallbackDTO(code="private-code" * 1000, state="private-state")
    assert "private-" not in str(error.value)
    callback = McpAuthorizationCallbackDTO(code="private-code", state="private-state")
    assert "private-" not in repr(callback)
    assert callback.model_dump() == {"code": "private-code", "state": "private-state"}
    change = {"tool_id": "tool", "execution_enabled": True, "model_visible": False}
    assert ToolSelectionPatchRequest.model_validate({"changes": [change]}).changes[0].model_visible is False
    with pytest.raises(ValidationError, match="重复 tool_id"):
        ToolSelectionPatchRequest.model_validate({"changes": [change, change]})
    with pytest.raises(ValidationError, match="changes"):
        ToolSelectionPatchRequest(changes=[])
    with pytest.raises(ValidationError, match="repetitions"):
        ToolTestStartRequest(repetitions=21)


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
    "JobDTO", "JobDispatchSnapshotDTO", "SessionGoalDTO",
    "SessionForkRequest", "SessionForkResultDTO", "ForkSourceCaptureDTO", "SessionForkOwnerReference",
    "SessionCreateRequest", "SessionUpdateRequest", "SessionDTO", "SessionInformationSnapshotDTO",
    "SessionGenerationOriginDTO", "SessionInformationResourceDTO",
    "McpToolDTO", "ToolTestAttemptDTO",
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
