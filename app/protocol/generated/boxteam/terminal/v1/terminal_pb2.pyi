import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TerminalStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TERMINAL_STATUS_UNSPECIFIED: _ClassVar[TerminalStatus]
    TERMINAL_STATUS_CREATED: _ClassVar[TerminalStatus]
    TERMINAL_STATUS_RUNNING: _ClassVar[TerminalStatus]
    TERMINAL_STATUS_EXITED: _ClassVar[TerminalStatus]
    TERMINAL_STATUS_TERMINATED: _ClassVar[TerminalStatus]
    TERMINAL_STATUS_DELETED: _ClassVar[TerminalStatus]
TERMINAL_STATUS_UNSPECIFIED: TerminalStatus
TERMINAL_STATUS_CREATED: TerminalStatus
TERMINAL_STATUS_RUNNING: TerminalStatus
TERMINAL_STATUS_EXITED: TerminalStatus
TERMINAL_STATUS_TERMINATED: TerminalStatus
TERMINAL_STATUS_DELETED: TerminalStatus

class TerminalSession(_message.Message):
    __slots__ = ("terminal_id", "title", "cols", "rows", "status", "sequence", "metadata", "created_at", "updated_at", "workspace_id", "cwd_relative", "transport_owner_revision", "exit_code", "last_command_exit_code", "command_start_sequence")
    TERMINAL_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    CWD_RELATIVE_FIELD_NUMBER: _ClassVar[int]
    TRANSPORT_OWNER_REVISION_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    LAST_COMMAND_EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    COMMAND_START_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    terminal_id: str
    title: str
    cols: int
    rows: int
    status: TerminalStatus
    sequence: int
    metadata: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    workspace_id: str
    cwd_relative: str
    transport_owner_revision: int
    exit_code: int
    last_command_exit_code: int
    command_start_sequence: int
    def __init__(self, terminal_id: _Optional[str] = ..., title: _Optional[str] = ..., cols: _Optional[int] = ..., rows: _Optional[int] = ..., status: _Optional[_Union[TerminalStatus, str]] = ..., sequence: _Optional[int] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., workspace_id: _Optional[str] = ..., cwd_relative: _Optional[str] = ..., transport_owner_revision: _Optional[int] = ..., exit_code: _Optional[int] = ..., last_command_exit_code: _Optional[int] = ..., command_start_sequence: _Optional[int] = ...) -> None: ...

class TerminalJsonMessage(_message.Message):
    __slots__ = ("type", "payload")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    type: str
    payload: _struct_pb2.Struct
    def __init__(self, type: _Optional[str] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
