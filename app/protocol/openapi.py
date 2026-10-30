from __future__ import annotations

from collections.abc import Mapping

from google.protobuf.descriptor import Descriptor
from google.protobuf.descriptor_pool import DescriptorPool
from pydantic.json_schema import JsonSchemaValue

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
    for name in ("ResourceNavigationRequest", "ResourceNavigationResultDTO")
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
    # 此 DTO 经具名 adapter 改变 JSON 外形，不能按同形结构直接核对。
    # TODO: 后续逐条迁齐其公开结构；保留内部 Pydantic 的业务职责。
    "SessionExecutionSseDTO": session_stream_pb2.DESCRIPTOR.message_types_by_name["SessionExecutionSse"],
}


def protobuf_message_name_for_model(model_name: str) -> str | None:
    descriptor = _SSE_DESCRIPTORS.get(model_name)
    return descriptor.full_name if descriptor is not None else None


def assert_public_http_schemas(document: JsonSchemaValue, pool: DescriptorPool | None = None) -> None:
    """核对真实 OpenAPI 组件，保留原模型的业务约束和 HTTP 外形。"""
    components = document["components"]
    schemas = components["schemas"]
    for name, descriptor in HTTP_JSON_DESCRIPTORS.items():
        if name not in schemas:
            raise ValueError(f"公开 HTTP 结构未被导出: {name}")
        if pool is not None:
            descriptor = pool.FindMessageTypeByName(descriptor.full_name)
        assert_public_schema({**schemas[name], "components": components}, descriptor)
