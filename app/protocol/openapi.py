from __future__ import annotations

from collections.abc import Mapping

from google.protobuf.descriptor import Descriptor

from app.protocol.generated.boxteam.workspace.v2 import (
    file_events_pb2,
    session_stream_pb2,
    trace_pb2,
)

_SSE_DESCRIPTORS: Mapping[str, Descriptor] = {
    "SessionExecutionSseDTO": session_stream_pb2.SessionExecutionSse.DESCRIPTOR,
    "TraceEventDTO": trace_pb2.TraceEvent.DESCRIPTOR,
    "WorkspaceFileChangeBatchDTO": file_events_pb2.WorkspaceFileChangeBatch.DESCRIPTOR,
    "SseErrorDTO": file_events_pb2.SseError.DESCRIPTOR,
}


def protobuf_message_name_for_model(model_name: str) -> str | None:
    descriptor = _SSE_DESCRIPTORS.get(model_name)
    return descriptor.full_name if descriptor is not None else None
