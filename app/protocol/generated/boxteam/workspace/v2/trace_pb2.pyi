import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TraceEvent(_message.Message):
    __slots__ = ("event_id", "part_id", "session_id", "job_id", "type", "phase", "title", "content", "status", "tool_name", "skill_names", "step_id", "timestamp", "raw", "thread_id")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    PART_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    SKILL_NAMES_FIELD_NUMBER: _ClassVar[int]
    STEP_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    part_id: str
    session_id: str
    job_id: str
    type: str
    phase: str
    title: str
    content: str
    status: str
    tool_name: str
    skill_names: _containers.RepeatedScalarFieldContainer[str]
    step_id: str
    timestamp: _timestamp_pb2.Timestamp
    raw: _struct_pb2.Struct
    thread_id: str
    def __init__(self, event_id: _Optional[str] = ..., part_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., type: _Optional[str] = ..., phase: _Optional[str] = ..., title: _Optional[str] = ..., content: _Optional[str] = ..., status: _Optional[str] = ..., tool_name: _Optional[str] = ..., skill_names: _Optional[_Iterable[str]] = ..., step_id: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...
