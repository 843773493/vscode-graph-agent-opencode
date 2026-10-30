from __future__ import annotations

from collections.abc import Mapping

from google.protobuf.descriptor import Descriptor

from app.protocol.generated.boxteam.workspace.v2 import (
    file_events_pb2,
    public_pb2,
    session_stream_pb2,
    trace_pb2,
)

SSE_JSON_DESCRIPTORS: Mapping[str, Descriptor] = {
    "TraceEventDTO": trace_pb2.DESCRIPTOR.message_types_by_name["TraceEvent"],
    "WorkspaceFileChangeBatchDTO": file_events_pb2.DESCRIPTOR.message_types_by_name["WorkspaceFileChangeBatch"],
    "SseErrorDTO": file_events_pb2.DESCRIPTOR.message_types_by_name["SseError"],
    "WorkspaceObservationFrameDTO": public_pb2.DESCRIPTOR.message_types_by_name["WorkspaceObservationFrameDTO"],
    "PersistentTaskListDTO": public_pb2.DESCRIPTOR.message_types_by_name["PersistentTaskListDTO"],
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
