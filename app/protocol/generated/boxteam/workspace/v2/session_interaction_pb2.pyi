import datetime

from boxteam.workspace.v2 import job_pb2 as _job_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionExecutionEventHeader(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class MessageObservation(_message.Message):
    __slots__ = ("message_id", "session_id", "role", "content", "attachments", "metadata", "created_at", "thread_id")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    session_id: str
    role: str
    content: str
    attachments: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Value]
    metadata: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    thread_id: str
    def __init__(self, message_id: _Optional[str] = ..., session_id: _Optional[str] = ..., role: _Optional[str] = ..., content: _Optional[str] = ..., attachments: _Optional[_Iterable[_Union[_struct_pb2.Value, _Mapping]]] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class JobStepProgress(_message.Message):
    __slots__ = ("agent_id", "message", "phase")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    message: str
    phase: str
    def __init__(self, agent_id: _Optional[str] = ..., message: _Optional[str] = ..., phase: _Optional[str] = ...) -> None: ...

class SessionStatus(_message.Message):
    __slots__ = ("session_id", "status", "message", "active_job_id", "observation_state", "is_streaming", "is_idle", "waiting")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_STATE_FIELD_NUMBER: _ClassVar[int]
    IS_STREAMING_FIELD_NUMBER: _ClassVar[int]
    IS_IDLE_FIELD_NUMBER: _ClassVar[int]
    WAITING_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    status: str
    message: str
    active_job_id: str
    observation_state: str
    is_streaming: bool
    is_idle: bool
    waiting: _struct_pb2.Struct
    def __init__(self, session_id: _Optional[str] = ..., status: _Optional[str] = ..., message: _Optional[str] = ..., active_job_id: _Optional[str] = ..., observation_state: _Optional[str] = ..., is_streaming: _Optional[bool] = ..., is_idle: _Optional[bool] = ..., waiting: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SessionError(_message.Message):
    __slots__ = ("error",)
    ERROR_FIELD_NUMBER: _ClassVar[int]
    error: str
    def __init__(self, error: _Optional[str] = ...) -> None: ...

class TraceObserved(_message.Message):
    __slots__ = ("raw_type",)
    RAW_TYPE_FIELD_NUMBER: _ClassVar[int]
    raw_type: str
    def __init__(self, raw_type: _Optional[str] = ...) -> None: ...

class SessionExecutionEvent(_message.Message):
    __slots__ = ("type", "header", "message_updated", "job_updated", "job_step_updated", "job_status_changed", "session_status_changed", "session_completed", "session_error", "trace_observed")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    HEADER_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_UPDATED_FIELD_NUMBER: _ClassVar[int]
    JOB_UPDATED_FIELD_NUMBER: _ClassVar[int]
    JOB_STEP_UPDATED_FIELD_NUMBER: _ClassVar[int]
    JOB_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    SESSION_STATUS_CHANGED_FIELD_NUMBER: _ClassVar[int]
    SESSION_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    SESSION_ERROR_FIELD_NUMBER: _ClassVar[int]
    TRACE_OBSERVED_FIELD_NUMBER: _ClassVar[int]
    type: str
    header: SessionExecutionEventHeader
    message_updated: MessageObservation
    job_updated: _job_pb2.JobProgress
    job_step_updated: JobStepProgress
    job_status_changed: _job_pb2.JobProgress
    session_status_changed: SessionStatus
    session_completed: _job_pb2.JobProgress
    session_error: SessionError
    trace_observed: TraceObserved
    def __init__(self, type: _Optional[str] = ..., header: _Optional[_Union[SessionExecutionEventHeader, _Mapping]] = ..., message_updated: _Optional[_Union[MessageObservation, _Mapping]] = ..., job_updated: _Optional[_Union[_job_pb2.JobProgress, _Mapping]] = ..., job_step_updated: _Optional[_Union[JobStepProgress, _Mapping]] = ..., job_status_changed: _Optional[_Union[_job_pb2.JobProgress, _Mapping]] = ..., session_status_changed: _Optional[_Union[SessionStatus, _Mapping]] = ..., session_completed: _Optional[_Union[_job_pb2.JobProgress, _Mapping]] = ..., session_error: _Optional[_Union[SessionError, _Mapping]] = ..., trace_observed: _Optional[_Union[TraceObserved, _Mapping]] = ...) -> None: ...
