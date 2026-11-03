from __future__ import annotations

from collections.abc import Mapping

from google.protobuf.descriptor import Descriptor
from google.protobuf.descriptor_pool import Default, DescriptorPool
from pydantic.json_schema import JsonSchemaValue

from app.protocol.generated.boxteam.gateway.v1 import public_pb2 as gateway_public_pb2
from app.protocol.generated.boxteam.search.v1 import public_pb2 as search_public_pb2
from app.protocol.generated.boxteam.workspace.message.v1 import message_stream_pb2
from app.protocol.generated.boxteam.workspace.v2 import (
    file_events_pb2,
    job_pb2,
    public_pb2,
    session_interaction_pb2,
    session_stream_pb2,
    trace_pb2,
)
from app.protocol.schema import assert_public_schema

# HTTP 根登记完整消息名，在发布时从本次 descriptor 解析；新增消息不依赖旧生成绑定。
# 信封合并及中断别名仍在原具名投影中核对。
MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS: dict[str, str] = {
    f"MessageStream{suffix}DTO": f"{message_stream_pb2.DESCRIPTOR.package}.{message}"
    for suffix, message in (
        ("Failure", "StreamFailure"),
        ("BlockSnapshot", "MessageBlockSnapshot"),
        ("ToolExecutionSnapshot", "ToolExecutionSnapshot"),
        ("ToolCall", "ToolCall"),
        ("ModelCallSnapshot", "ModelCallSnapshot"),
        ("Activity", "Activity"),
        ("ResourceRef", "ResourceRef"),
        ("ActiveState", "ActiveState"),
        ("InterruptState", "InterruptState"),
        ("RecoveryState", "RecoveryState"),
    )
}

# 只登记已迁齐的 HTTP 公开模型，嵌套字段由 descriptor 递归校验。
HTTP_JSON_DESCRIPTORS: Mapping[str, str] = {
    name: f"{public_pb2.DESCRIPTOR.package}.{name}"
    for name in (
        "AgentDTO", "WorkspaceDefaultAgentUpdateRequest", "WorkspaceDefaultProviderUpdateRequest",
        "ResourceNavigationRequest", "ResourceNavigationResultDTO",
        "WorkspaceDTO", "WorkspaceContextDTO", "WorkspaceIndexStatusDTO", "WorkspaceIndexRebuildDTO",
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
        "SessionThreadSearchResultsDTO",
        "SessionCatalogPageDTO", "SessionCatalogBreadcrumbDTO",
        "SessionCatalogSearchResultsDTO", "SessionCatalogExportDTO",
        "WorkspaceSearchCapabilitiesDTO", "WorkspaceSearchRequest", "WorkspaceSearchResultDTO",
        "SessionTurnBootstrapDTO", "TurnHistoryLoadRequest", "TurnHistoryPageDTO",
        "TurnMessageSearchRequest", "TurnMessageSearchResultsDTO",
        "WorkspaceMessageSearchResultsDTO",
        "LLMRequestLogReferenceDTO", "LLMRequestLogLocationDTO", "WorkspaceLLMRequestLogSearchResultsDTO",
        "SessionChangesetListDTO", "SessionChangesetDTO", "SessionFileReviewRequest", "SessionFileReviewResultDTO",
        "SessionContextReadRequest", "SessionContextReadResultDTO", "SessionContextSearchRequest", "SessionContextSearchResultDTO",
        "SessionSkillUntrackRequest", "SessionSkillUntrackResultDTO",
        "McpServerDTO", "McpConnectionOperationDTO", "McpConnectionRequestDTO",
        "McpAuthorizationDTO", "McpAuthorizationCallbackDTO",
        "ToolDTO", "ToolSelectionPatchRequest", "ToolTestStartRequest", "ToolTestRunDTO", "ToolTestRunListDTO",
        "PersistentTaskCreateRequest", "PersistentTaskUpdateRequest", "PersistentTaskControlRequest",
        "PersistentTaskCheckRequest", "PersistentTaskNotificationRequest", "SessionGeneratorImportRequest",
        "PersistentTaskDTO", "PersistentTaskListDTO", "PersistentTaskRunDTO",
        "PersistentTaskRunListDTO", "PersistentTaskRunDetailDTO",
        "SessionGenerationPreviewRequest", "SessionGenerationPreviewDTO",
        "SessionGenerationValidateRequest", "SessionGenerationValidationDTO",
        "RuntimeStatusDTO", "RuntimeDrainResultDTO", "UiSnapshotResultDTO",
    )
} | MESSAGE_STREAM_HTTP_JSON_DESCRIPTORS

GATEWAY_HTTP_JSON_DESCRIPTORS: Mapping[str, str] = {
    name: f"{gateway_public_pb2.DESCRIPTOR.package}.{name}"
    for name in (
        "GatewayDirectoryListDTO", "NativeDirectoryBindRequest",
        "NativeDirectoryBindingDTO", "NativeDirectoryResolveDTO",
        "GatewayResourceAddressRequest", "GatewayResourceAddressDTO",
        "GatewayManagedWorkspaceListDTO", "CreateGatewayManagedWorkspaceRequest",
        "CreateFederationManagedWorkspaceRequest",
        "SshConnectionOptionListDTO", "AddRemoteGatewayRequest",
        "PortForwardListDTO", "CreatePortForwardRequest",
        "ChangePortForwardLocalPortRequest", "ChangePortForwardLabelRequest",
        "GatewayUserListDTO", "GatewayUserDTO", "GatewayUserAccessDTO",
        "CreateGatewayUserRequest", "AcquireGatewayUserRequest", "CreateGatewayGuestRequest",
        "GatewayHealthDTO", "GatewayDiagnosticsDTO", "DevelopmentRuntimeRestartDTO",
        "GatewayServiceStatusDTO", "GatewayRuntimeBlockerDTO",
        "GatewayConfigSourcesDTO", "GatewayConfigReloadStatusDTO", "GatewayConfigEventsDTO",
        "GatewayConfigPendingHealthProofRequest", "GatewayConfigPendingDiscardRequest",
        "FederationProtocolManifestDTO", "FederationWorkspaceListDTO", "GatewayInboundAccessListDTO",
        "GatewayWorkspaceListDTO", "GatewayRuntimeStateResultDTO", "GatewayRuntimeRestartResultDTO",
        "GatewayResourceListDTO",
        "GatewayWorkspaceDiscoveryResultsDTO",
        "GatewaySearchScopeRequest", "GatewaySearchScopeResolutionDTO",
        "AddLocalWorkspaceRequest", "UpdateGatewayWorkspaceRequest", "ReorderGatewayWorkspacesRequest",
        "ActivateGatewayWorkspaceResultDTO", "WorkspaceFolderCreateRequest", "WorkspaceNavigationNodeUpdateRequest",
        "WorkspaceNavigationPlacementRequest", "WorkspaceNavigationReorderRequest",
        "WorkspaceNavigationTreeDTO", "WorkspaceNavigationBreadcrumbDTO",
    )
} | {name: f"{search_public_pb2.DESCRIPTOR.package}.{name}" for name in ("FederatedSearchRequest", "FederatedSearchEventDTO")}

SSE_JSON_DESCRIPTORS: Mapping[str, Descriptor] = {
    "FederatedSearchEventDTO": search_public_pb2.DESCRIPTOR.message_types_by_name["FederatedSearchEventDTO"],
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
    document: JsonSchemaValue, descriptors: Mapping[str, str], pool: DescriptorPool | None,
) -> None:
    """两类服务共用同一结构检查，嵌套字段仍由 descriptor 递归推导。"""
    components = document["components"]
    schemas = components["schemas"]
    # 先核对全部根，避免较早的嵌套引用将缺失模型误报成 KeyError。
    for name in descriptors:
        if name not in schemas:
            raise ValueError(f"公开 HTTP 结构未被导出: {name}")
    descriptor_pool = pool if pool is not None else Default()
    for name, message_name in descriptors.items():
        descriptor = descriptor_pool.FindMessageTypeByName(message_name)
        assert_public_schema({**schemas[name], "components": components}, descriptor)


def assert_gateway_http_schemas(document: JsonSchemaValue, pool: DescriptorPool | None = None) -> None:
    """核对 Gateway 控制面公开结构，保留原生目录输入及 URI 业务校验。"""
    _assert_http_schemas(document, GATEWAY_HTTP_JSON_DESCRIPTORS, pool)
    name = "GatewayRuntimeGroupRequest"
    components = document["components"]
    schemas = components["schemas"]
    if name not in schemas:
        raise ValueError(f"公开 HTTP 结构未被导出: {name}")
    descriptor = gateway_public_pb2.DESCRIPTOR.message_types_by_name[name]
    if pool is not None:
        descriptor = pool.FindMessageTypeByName(descriptor.full_name)
    # HTTP 的可空数组保留原外形；仅在此投影到保留 presence 的 proto 包装消息。
    # 不忽略根或包装消息的成员，其余字段仍由 descriptor 递归核对。
    properties = {
        field: {"type": "object", "properties": {"workspace_ids": value}}
        if field == "desired_workspace_ids" else value
        for field, value in schemas[name]["properties"].items()
    }
    assert_public_schema({**schemas[name], "properties": properties, "components": components}, descriptor)


def assert_public_http_schemas(document: JsonSchemaValue, pool: DescriptorPool | None = None) -> None:
    """核对真实 OpenAPI 组件，保留原模型的业务约束和 HTTP 外形。"""
    _assert_http_schemas(document, HTTP_JSON_DESCRIPTORS, pool)
    components = document["components"]
    schemas = components["schemas"]
    _assert_message_stream_snapshot(schemas, components, pool)
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


def _assert_message_stream_snapshot(
    schemas: JsonSchemaValue, components: JsonSchemaValue, pool: DescriptorPool | None,
) -> None:
    """HTTP 根展平事件定位字段，并把旧 wire 中断别名对应到现行嵌套字段。"""
    name = "MessageStreamSnapshotDTO"
    if name not in schemas:
        raise ValueError(f"公开 HTTP 结构未被导出: {name}")
    snapshot = message_stream_pb2.DESCRIPTOR.message_types_by_name["StreamSnapshot"]
    event = message_stream_pb2.DESCRIPTOR.message_types_by_name["MessageStreamEvent"]
    if pool is not None:
        snapshot = pool.FindMessageTypeByName(snapshot.full_name)
        event = pool.FindMessageTypeByName(event.full_name)
    fields = dict(snapshot.fields_by_name)
    fields.update({
        field: event.fields_by_name[field]
        for field in ("session_id", "thread_id", "turn_id", "turn_stream_id")
    })
    # 不跳过旧 wire 字段；从同一公开中断对象取外形，仍核其真实 descriptor 类型。
    interrupt = schemas["MessageStreamInterruptStateDTO"]["properties"]
    aliases = {"interrupt_request_id": "request_id", "interrupt_status": "status"}
    if set(aliases) & schemas[name]["properties"].keys():
        raise ValueError("消息流 HTTP 根不接受 wire 中断别名，必须使用 interrupt_state")
    properties = {
        **schemas[name]["properties"],
        **{wire: interrupt[public] for wire, public in aliases.items()},
    }
    assert_public_schema(
        {**schemas[name], "properties": properties, "components": components},
        snapshot, root_fields=fields,
    )
