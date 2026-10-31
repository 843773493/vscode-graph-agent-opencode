"""普通 HTTP 结构与本次编译的 proto 一致，错误时不得发布快照。"""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
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
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskControlRequest,
    TaskConditionDTO,
    TaskTriggerDTO,
)
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationNodeDTO,
    ResourceNavigationRequest,
)
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionInformationSessionDTO,
    SessionSkillUntrackRequest,
    SessionSkillUntrackResultDTO,
)
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextResourceDTO,
    SessionContextSearchRequest,
)
from app.schemas.internal_v2.text_mutation import (
    CreateText,
    TextMutationReceiptDTO,
    TextMutationRequest,
    TextMutationSnapshotDTO,
)
from app.schemas.internal_v2.tool import ToolSelectionPatchRequest
from app.schemas.internal_v2.tool_test import ToolTestStartRequest
from app.schemas.internal_v2.workspace_resource import (
    BrowserResourceOperationRequest,
    TerminalResourceCreateRequest,
    WorkspaceResourceCommandRequest,
)
from tests.contracts.api.openapi_snapshots import OPENAPI_SNAPSHOTS


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
    ("AgentDTO", "tools", {"type": "object"}),
    ("AgentDTO", "providers", {"type": "object"}),
    ("AgentProviderDTO", "available", {"type": "string"}),
    ("AgentProviderDTO", "configuration_error", {"type": "integer"}),
    ("WorkspaceDefaultAgentUpdateRequest", "agent_id", {"type": "integer"}),
    ("WorkspaceDefaultProviderUpdateRequest", "provider_id", {"type": "integer"}),
])
def test_http_schema_recursively_rejects_real_component_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_agent_http_routes_register_their_actual_public_json_roots(http_openapi: JsonSchemaValue) -> None:
    original = deepcopy(http_openapi)
    schemas = http_openapi["components"]["schemas"]
    pending: list[JsonSchemaValue] = []
    prefix = "/api/v1/workspaces/{workspace_id}/agents"
    for path, method, request_model in (
        (prefix, "get", None),
        (prefix + "/{agent_id}", "get", None),
        (prefix + "/workspace-default", "put", "WorkspaceDefaultAgentUpdateRequest"),
        (prefix + "/{agent_id}/workspace-default-provider", "put", "WorkspaceDefaultProviderUpdateRequest"),
    ):
        operation = http_openapi["paths"][path][method]
        if request_model is not None:
            request = operation["requestBody"]["content"]["application/json"]["schema"]
            assert request["$ref"].endswith("/" + request_model)
            pending.append(request)
        response = operation["responses"]["200"]["content"]["application/json"]["schema"]
        envelope = schemas[response["$ref"].rsplit("/", 1)[1]]
        data = envelope["properties"]["data"]["anyOf"][0]
        assert (data if path.endswith("/{agent_id}") else data["items"])["$ref"].endswith("/AgentDTO")
        pending.append(response)
    roots = _public_roots(http_openapi, pending)
    assert roots == {"AgentDTO", "WorkspaceDefaultAgentUpdateRequest", "WorkspaceDefaultProviderUpdateRequest"}
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"Agent 入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original


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


def test_persistent_task_http_routes_check_definition_control_run_and_import_roots(
    http_openapi: JsonSchemaValue,
) -> None:
    prefix = "/api/v1/workspaces/{workspace_id}/persistent-tasks"
    pending: list[JsonSchemaValue] = []
    for path, operations in http_openapi["paths"].items():
        if path != prefix and not path.startswith(prefix + "/"):
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
        "PersistentTaskCreateRequest", "PersistentTaskUpdateRequest", "PersistentTaskControlRequest",
        "PersistentTaskCheckRequest", "PersistentTaskNotificationRequest", "SessionGeneratorImportRequest",
        "PersistentTaskDTO", "PersistentTaskListDTO", "PersistentTaskRunDTO",
        "PersistentTaskRunListDTO", "PersistentTaskRunDetailDTO",
    }
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"持久任务入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("PersistentTaskControlRequest", "expected_revision", {"type": "string"}),
    ("PersistentTaskDTO", "application_type", {"type": "integer"}),
    ("TaskTriggerDTO", "interval_seconds", {"type": "string"}),
    ("RuntimeSourceActionDTO", "target_uri", {"type": "integer"}),
    ("SessionGenerationActionDTO", "generation", {"type": "string"}),
    ("TaskInputConsumptionDTO", "request_hash", {"type": "integer"}),
    ("TaskRuntimeStatusDTO", "recovering_tasks", {"type": "string"}),
    ("TaskScriptCheckDTO", "command", {"type": "object"}),
    ("SessionGeneratorImportRequest", "workspace_ids", {"type": "array", "items": {}}),
])
def test_persistent_task_http_schema_rejects_definition_union_run_and_import_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_persistent_task_structure_checks_preserve_pydantic_schedule_component_and_revision_rules(
    http_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    with pytest.raises(ValidationError, match="非时间触发不能配置时间计划"):
        TaskTriggerDTO(version="1", type_id="builtin.manual", interval_seconds=10)
    with pytest.raises(ValidationError, match="判断器必须配置对应参数"):
        TaskConditionDTO(version="1", type_id="builtin.script")
    with pytest.raises(ValidationError, match="expected_revision"):
        PersistentTaskControlRequest(expected_revision=0, action="pause")


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


def test_text_mutation_http_route_checks_actual_command_and_transfer_roots(http_openapi: JsonSchemaValue) -> None:
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}/resources/text-mutations"]["post"]
    roots = _public_roots(http_openapi, [
        operation["requestBody"]["content"]["application/json"]["schema"],
        operation["responses"]["200"]["content"]["application/json"]["schema"],
    ])
    assert roots == {"TextMutationRequest", "TextMutationTransferDTO"}
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"正文修改入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("TextMutationRequest", "timeout_ms", {"type": "string"}),
    ("CreateText", "content", {"type": "integer"}),
    ("EditText", "replace_all", {"type": "string"}),
    ("PatchText", "expected_versions", {"type": "object", "additionalProperties": {"type": "integer"}}),
    ("TextMutationTransferDTO", "snapshots", {"type": "object"}),
    ("TextMutationReceiptDTO", "retryable", {"type": "integer"}),
    ("TextMutationFileDTO", "exists", {"type": "string"}),
    ("TextMutationSnapshotDTO", "after_exists", {"type": "string"}),
])
def test_text_mutation_http_schema_rejects_command_map_receipt_and_snapshot_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_text_mutation_structure_checks_preserve_command_uri_version_and_postcondition_rules(
    http_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    uri = "boxteam://workspace/ws-1/resources/files/root/result.txt"
    command = CreateText(target=uri, content="/etc/hosts", expected_version="absent")
    assert TextMutationRequest(create=command).command == command
    with pytest.raises(ValidationError, match="恰好提供"):
        TextMutationRequest()
    with pytest.raises(ValidationError, match="恰好提供"):
        TextMutationRequest.model_validate({"create": command.model_dump(), "patch": {
            "target": uri, "input": "patch", "expected_versions": {}, "explanation": "修改",
        }})
    # 普通工具接受原生目标；传输封套仍要求目标owner已解析的固定URI。
    native = CreateText(target="/etc/hosts", content="正文", expected_version="absent")
    with pytest.raises(ValidationError, match="URI"):
        TextMutationRequest(create=native)
    with pytest.raises(ValidationError, match="expected_version"):
        CreateText(target=uri, content="正文", expected_version="latest")
    with pytest.raises(ValidationError, match="timeout_ms"):
        TextMutationRequest(create=command, timeout_ms=30001)
    with pytest.raises(ValidationError, match="URI"):
        TextMutationReceiptDTO(status="success", effect="completed", target="/etc/hosts")
    with pytest.raises(ValidationError, match="retryable"):
        TextMutationReceiptDTO.model_validate({"status": "error", "effect": "unknown", "retryable": True})
    with pytest.raises(ValidationError, match="缺少可核后件"):
        TextMutationSnapshotDTO(uri=uri, before_exists=True, before_content="正文")
    snapshot = TextMutationSnapshotDTO(uri=uri, before_exists=True, before_content="/etc/hosts",
                                       after_exists=False, after_content="")
    assert snapshot.before_content == "/etc/hosts" and snapshot.after_exists is False


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


@pytest.mark.parametrize(("path", "expected"), [
    ("/context/read", {"SessionContextReadRequest", "SessionContextReadResultDTO"}),
    ("/context/search", {"SessionContextSearchRequest", "SessionContextSearchResultDTO"}),
    ("/sessions/{session_id}/threads/{thread_id}/skills/untrack", {"SessionSkillUntrackRequest", "SessionSkillUntrackResultDTO"}),
])
def test_context_http_routes_check_actual_request_and_response_roots(
    http_openapi: JsonSchemaValue, path: str, expected: set[str],
) -> None:
    operation = http_openapi["paths"]["/api/v1/workspaces/{workspace_id}" + path]["post"]
    roots = _public_roots(http_openapi, [
        operation["requestBody"]["content"]["application/json"]["schema"],
        operation["responses"]["200"]["content"]["application/json"]["schema"],
    ])
    assert roots == expected
    assert roots <= HTTP_JSON_DESCRIPTORS.keys(), f"上下文入口缺少 proto 结构校验: {sorted(roots - HTTP_JSON_DESCRIPTORS.keys())}"
    assert_public_http_schemas(http_openapi)


@pytest.mark.parametrize(("model", "field", "shape"), [
    ("SessionContextReadRequest", "include", {"type": "array", "items": {"type": "integer"}}),
    ("SessionContextItemDTO", "resource_refs", {"type": "object"}),
    ("SessionContextProvenanceRefDTO", "token", {"type": "integer"}),
    ("SessionContextReadResultDTO", "returned_chars", {"type": "string"}),
    ("SessionContextSearchMatchDTO", "match_start", {"type": "string"}),
    ("SessionContextSearchResultDTO", "partial_errors", {"type": "array", "items": {"type": "string"}}),
    ("SessionSkillUntrackErrorDTO", "code", {"type": "integer"}),
    ("SessionSkillUntrackResultDTO", "queued", {"type": "string"}),
])
def test_context_http_schema_rejects_nested_source_search_and_untrack_drift(
    http_openapi: JsonSchemaValue, model: str, field: str, shape: JsonSchemaValue,
) -> None:
    http_openapi["components"]["schemas"][model]["properties"][field] = shape
    with pytest.raises(ValueError, match="公开结构与 proto 类型不一致") as error:
        assert_public_http_schemas(http_openapi)
    assert f"/{field}" in str(error.value)


def test_context_http_structure_checks_preserve_identity_uri_budget_and_skill_rules(
    http_openapi: JsonSchemaValue,
) -> None:
    original = deepcopy(http_openapi)
    assert_public_http_schemas(http_openapi)
    assert http_openapi == original
    request = {"resource_identity": "context-resource", "uri": "selected-resource", "scope": "workspace"}
    # 请求定位和授权仍交原 owner；结构检查不替代业务约束或解析当前资源。
    assert SessionContextReadRequest.model_validate(request).uri == "selected-resource"
    with pytest.raises(ValidationError, match="resource_identity"):
        SessionContextReadRequest.model_validate({**request, "resource_identity": "/private/context"})
    with pytest.raises(ValidationError, match="max_chars"):
        SessionContextReadRequest.model_validate({**request, "max_chars": 255})
    with pytest.raises(ValidationError, match="query"):
        SessionContextSearchRequest.model_validate({**request, "query": ""})
    digest = "sha256:jcs:v1:" + "a" * 64
    resource = {"display_uri": None, "resource_kind": "skills", "owner_scope": "workspace", "facet": "content",
                "revision": digest, "availability": "available", "provenance_ref": {"token": digest}}
    assert SessionContextResourceDTO.model_validate(resource).model_dump(mode="json")["display_uri"] is None
    with pytest.raises(ValidationError, match="display_uri"):
        SessionContextResourceDTO.model_validate({**resource, "display_uri": "/private/context"})
    with pytest.raises(ValidationError, match="name"):
        SessionSkillUntrackRequest(name="")
    result = {"session_id": "session-1", "thread_id": "thread-1", "name": "demo", "mode": "untrack", "status": "not_tracked"}
    assert SessionSkillUntrackResultDTO.model_validate(result).display_uri is None
    with pytest.raises(ValidationError, match="mode"):
        SessionSkillUntrackResultDTO.model_validate({**result, "mode": "track"})


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
    "PersistentTaskCreateRequest", "TaskInputConsumptionDTO", "TaskActionDTO", "SessionGeneratorImportRequest",
    "TextMutationRequest", "CreateText", "EditText", "PatchText",
    "TextMutationTransferDTO", "TextMutationReceiptDTO", "TextMutationFileDTO", "TextMutationSnapshotDTO",
    "SessionContextReadRequest", "SessionContextResourceDTO", "SessionContextProvenanceRefDTO",
    "SessionContextSearchMatchDTO", "SessionSkillUntrackResultDTO",
    "AgentDTO", "AgentProviderDTO", "WorkspaceDefaultAgentUpdateRequest", "WorkspaceDefaultProviderUpdateRequest",
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
    for name in OPENAPI_SNAPSHOTS:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original snapshot\n")
    result = _export(output, "--descriptor-set", str(descriptor_set))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "公开结构与 proto 字段不一致" in result.stderr and "new_public_field" in result.stderr
    assert all((output / name).read_bytes() == b"original snapshot\n" for name in OPENAPI_SNAPSHOTS)


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
    contents = [(output / name).read_bytes() for name in OPENAPI_SNAPSHOTS]
    assert contents[0] == contents[1]
    assert json.loads(contents[0]) == http_openapi
