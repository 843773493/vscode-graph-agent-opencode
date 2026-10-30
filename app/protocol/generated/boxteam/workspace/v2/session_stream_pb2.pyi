from boxteam.workspace.v2 import session_interaction_pb2 as _session_interaction_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SessionExecutionSse(_message.Message):
    __slots__ = ("event", "raw_type", "raw_payload")
    EVENT_FIELD_NUMBER: _ClassVar[int]
    RAW_TYPE_FIELD_NUMBER: _ClassVar[int]
    RAW_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event: _session_interaction_pb2.SessionExecutionEvent
    raw_type: str
    raw_payload: _struct_pb2.Struct
    def __init__(self, event: _Optional[_Union[_session_interaction_pb2.SessionExecutionEvent, _Mapping]] = ..., raw_type: _Optional[str] = ..., raw_payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
