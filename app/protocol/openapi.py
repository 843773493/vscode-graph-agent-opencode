from __future__ import annotations

from collections.abc import Mapping

from google.protobuf.descriptor import Descriptor
from google.protobuf.descriptor_pool import DescriptorPool
from pydantic.json_schema import JsonSchemaValue

from app.protocol.generated.boxteam.gateway.v1 import public_pb2 as gateway_public_pb2
from app.protocol.generated.boxteam.workspace.v2 import (
    file_events_pb2,
    job_pb2,
    public_pb2,
    session_interaction_pb2,
    session_stream_pb2,
    trace_pb2,
)
from app.protocol.schema import assert_public_schema

# 只登记本切片已迁齐的 HTTP 根模型，嵌套字段由 descriptor 递归校验。
HTTP_JSON_DESCRIPTORS: Mapping[str, Descriptor] = {
    name: public_pb2.DESCRIPTOR.message_types_by_name[name]
    for name in (
        "AgentDTO", "WorkspaceDefaultAgentUpdateRequest", "WorkspaceDefaultProviderUpdateRequest",
        "ResourceNavigationRequest", "ResourceNavigationResultDTO",
        "WorkspaceFileRootsDTO", "WorkspaceFileReferencesRequest", "WorkspaceFileReferencesDTO",
        "WorkspaceFileWatchRequest", "WorkspaceFileListDTO", "WorkspaceFileContentDTO",
        "WorkspaceFileUpdateRequest", "WorkspaceFileCreateRequest", "WorkspaceFilePasteRequest",
        "WorkspaceFileCopyRequest", "WorkspaceFileOperationReceiptDTO", "WorkspaceFileOperationStatusDTO",
        "WorkspaceFileRevealDTO", "FileTreeShortcutRequest", "SessionFileTreeSettingsDTO",
        "TextMutationRequest", "TextMutationTransferDTO",
        "NodeDebugCapabilitiesDTO", "NodeDebugStateDTO", "NodeDebugConfigurationDTO",
        "NodeDebugConfigurationCreateRequest", "NodeDebugConfigurationUpdateRequest",
        "NodeDebugConfigurationActivateRequest", "NodeDebugConfigurationImportRequest",
        "NodeDebugConfigurationCopyRequest", "NodeDebugStartRequest",
        "WorkspaceResourceDTO", "WorkspaceResourceListDTO", "TerminalResourceCreateRequest",
        "BrowserResourceCreateRequest", "BrowserResourceOperationRequest",
        "WorkspaceResourceCommandRequest", "ResourceTransportGrantDTO",
        "SessionResourceListDTO", "SessionResourceControlRequest", "SessionResourceControlResultDTO",
        "JobDTO", "StepDTO", "JobControlRequest", "JobControlResponseDTO", "JobDispatchSnapshotDTO",
        "SessionGoalDTO", "SessionGoalSetRequest", "SessionGoalClearResultDTO",
        "SessionForkRequest", "SessionForkSourcePointDTO", "SessionForkResultDTO",
        "SessionForkTransferRequest", "SessionForkTransferResultDTO",
        "SessionCreateRequest", "SessionUpdateRequest", "SessionDTO", "SessionInformationSnapshotDTO",
        "SessionContextReadRequest", "SessionContextReadResultDTO", "SessionContextSearchRequest", "SessionContextSearchResultDTO",
        "SessionSkillUntrackRequest", "SessionSkillUntrackResultDTO",
        "McpServerDTO", "McpConnectionOperationDTO", "McpConnectionRequestDTO",
        "McpAuthorizationDTO", "McpAuthorizationCallbackDTO",
        "ToolDTO", "ToolSelectionPatchRequest", "ToolTestStartRequest", "ToolTestRunDTO", "ToolTestRunListDTO",
        "PersistentTaskCreateRequest", "PersistentTaskUpdateRequest", "PersistentTaskControlRequest",
        "PersistentTaskCheckRequest", "PersistentTaskNotificationRequest", "SessionGeneratorImportRequest",
        "PersistentTaskDTO", "PersistentTaskListDTO", "PersistentTaskRunDTO",
        "PersistentTaskRunListDTO", "PersistentTaskRunDetailDTO",
    )
}

GATEWAY_HTTP_JSON_DESCRIPTORS: Mapping[str, Descriptor] = {
    name: gateway_public_pb2.DESCRIPTOR.message_types_by_name[name]
    for name in (
        "GatewayDirectoryListDTO", "NativeDirectoryBindRequest",
        "NativeDirectoryBindingDTO", "NativeDirectoryResolveDTO",
        "GatewayManagedWorkspaceListDTO", "CreateGatewayManagedWorkspaceRequest",
        "CreateFederationManagedWorkspaceRequest",
    )
}

SSE_JSON_DESCRIPTORS: Mapping[str, Descriptor] = {
    "TraceEventDTO": trace_pb2.DESCRIPTOR.message_types_by_name["TraceEvent"],
    "WorkspaceFileChangeBatchDTO": file_events_pb2.DESCRIPTOR.message_types_by_name["WorkspaceFileChangeBatch"],
    "SseErrorDTO": file_events_pb2.DESCRIPTOR.message_types_by_name["SseError"],
    "WorkspaceObservationFrameDTO": public_pb2.DESCRIPTOR.message_types_by_name["WorkspaceObservationFrameDTO"],
    "PersistentTaskListDTO": public_pb2.DESCRIPTOR.message_types_by_name["PersistentTaskListDTO"],
    # Session SSE 的信封经具名 adapter 转换，以下 payload 本身仍与 proto 同形。
    "JobProgressDTO": job_pb2.DESCRIPTOR.message_types_by_name["JobProgress"],
    "JobStepProgressDTO": session_interaction_pb2.DESCRIPTOR.message_types_by_name["JobStepProgress"],
    "MessageObservationDTO": session_interaction_pb2.DESCRIPTOR.message_types_by_name["MessageObservation"],
    "SessionErrorPayloadDTO": session_interaction_pb2.DESCRIPTOR.message_types_by_name["SessionError"],
    "TraceObservedPayloadDTO": session_interaction_pb2.DESCRIPTOR.message_types_by_name["TraceObserved"],
}

_SSE_DESCRIPTORS: Mapping[str, Descriptor] = {
    **SSE_JSON_DESCRIPTORS,
    # 此 DTO 展平 header 并改变 payload 外形，由 SSE 导出入口的具名投影核对。
    "SessionExecutionSseDTO": session_stream_pb2.DESCRIPTOR.message_types_by_name["SessionExecutionSse"],
}


def protobuf_message_name_for_model(model_name: str) -> str | None:
    descriptor = _SSE_DESCRIPTORS.get(model_name)
    return descriptor.full_name if descriptor is not None else None


def _assert_http_schemas(
    document: JsonSchemaValue, descriptors: Mapping[str, Descriptor], pool: DescriptorPool | None,
) -> None:
    """两类服务共用同一结构检查，嵌套字段仍由 descriptor 递归推导。"""
    components = document["components"]
    schemas = components["schemas"]
    # 先核对全部根，避免较早的嵌套引用将缺失模型误报成 KeyError。
    for name in descriptors:
        if name not in schemas:
            raise ValueError(f"公开 HTTP 结构未被导出: {name}")
    for name, descriptor in descriptors.items():
        if pool is not None:
            descriptor = pool.FindMessageTypeByName(descriptor.full_name)
        assert_public_schema({**schemas[name], "components": components}, descriptor)


def assert_gateway_http_schemas(document: JsonSchemaValue, pool: DescriptorPool | None = None) -> None:
    """核对 Gateway 控制面公开结构，保留原生目录输入及 URI 业务校验。"""
    _assert_http_schemas(document, GATEWAY_HTTP_JSON_DESCRIPTORS, pool)


def assert_public_http_schemas(document: JsonSchemaValue, pool: DescriptorPool | None = None) -> None:
    """核对真实 OpenAPI 组件，保留原模型的业务约束和 HTTP 外形。"""
    _assert_http_schemas(document, HTTP_JSON_DESCRIPTORS, pool)
    components = document["components"]
    schemas = components["schemas"]
    # REST 的 action + params 是既有业务外形；只在此把外形投影到 proto oneof，
    # 动作成员、公共字段及嵌套参数仍由本次 descriptor 推导，不另维护字段表。
    descriptor = public_pb2.DESCRIPTOR.message_types_by_name["NodeDebugActionRequest"]
    if pool is not None:
        descriptor = pool.FindMessageTypeByName(descriptor.full_name)
    actions = {field.name for field in descriptor.oneofs_by_name["action"].fields}
    common = set(descriptor.fields_by_name) - actions
    action_operation = document["paths"]["/api/v1/workspaces/{workspace_id}/debug/node/action"]["post"]
    action_schema = action_operation["requestBody"]["content"]["application/json"]["schema"]
    discriminator = action_schema["discriminator"]
    mapping = discriminator["mapping"]
    if discriminator["propertyName"] != "action" or mapping.keys() != actions:
        raise ValueError("Node Debug HTTP 动作与 proto oneof 成员不一致")
    if {branch["$ref"] for branch in action_schema["oneOf"]} != set(mapping.values()):
        raise ValueError("Node Debug HTTP 动作分支与 discriminator 不一致")
    branches = []
    for action, reference in mapping.items():
        properties = schemas[reference.rsplit("/", 1)[1]]["properties"]
        if set(properties) != common | {"action", "params"}:
            raise ValueError(f"Node Debug HTTP 动作字段与 proto 不一致: {action}")
        declared = properties["action"]
        labels = declared.get("enum", [declared["const"]] if "const" in declared else [])
        if set(labels) != {name for name, target in mapping.items() if target == reference}:
            raise ValueError(f"Node Debug HTTP 动作标签与 discriminator 不一致: {action}")
        branches.append({"type": "object", "properties": {
            **{name: properties[name] for name in common}, action: properties["params"],
        }})
    assert_public_schema({"oneOf": branches, "components": components}, descriptor)
