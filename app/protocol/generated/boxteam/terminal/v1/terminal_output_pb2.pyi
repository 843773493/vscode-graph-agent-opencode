from boxteam.common.v1 import error_pb2 as _error_pb2
from boxteam.terminal.v1 import terminal_pb2 as _terminal_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TerminalOutputChunk(_message.Message):
    __slots__ = ("sequence", "data")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    data: str
    def __init__(self, sequence: _Optional[int] = ..., data: _Optional[str] = ...) -> None: ...

class TerminalStateChanged(_message.Message):
    __slots__ = ("status", "sequence")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    status: _terminal_pb2.TerminalStatus
    sequence: int
    def __init__(self, status: _Optional[_Union[_terminal_pb2.TerminalStatus, str]] = ..., sequence: _Optional[int] = ...) -> None: ...

class TerminalCompleted(_message.Message):
    __slots__ = ("exit_code", "signal")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    SIGNAL_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    signal: str
    def __init__(self, exit_code: _Optional[int] = ..., signal: _Optional[str] = ...) -> None: ...

class TerminalServerMessage(_message.Message):
    __slots__ = ("attached", "detached", "output", "state_changed", "completed", "error", "json")
    ATTACHED_FIELD_NUMBER: _ClassVar[int]
    DETACHED_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    STATE_CHANGED_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    attached: _terminal_pb2.TerminalSession
    detached: _terminal_pb2.TerminalSession
    output: TerminalOutputChunk
    state_changed: TerminalStateChanged
    completed: TerminalCompleted
    error: _error_pb2.Error
    json: _terminal_pb2.TerminalJsonMessage
    def __init__(self, attached: _Optional[_Union[_terminal_pb2.TerminalSession, _Mapping]] = ..., detached: _Optional[_Union[_terminal_pb2.TerminalSession, _Mapping]] = ..., output: _Optional[_Union[TerminalOutputChunk, _Mapping]] = ..., state_changed: _Optional[_Union[TerminalStateChanged, _Mapping]] = ..., completed: _Optional[_Union[TerminalCompleted, _Mapping]] = ..., error: _Optional[_Union[_error_pb2.Error, _Mapping]] = ..., json: _Optional[_Union[_terminal_pb2.TerminalJsonMessage, _Mapping]] = ...) -> None: ...
