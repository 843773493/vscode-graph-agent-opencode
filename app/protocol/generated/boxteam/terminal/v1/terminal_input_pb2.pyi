from boxteam.terminal.v1 import terminal_pb2 as _terminal_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TerminalAttach(_message.Message):
    __slots__ = ("terminal_id", "cols", "rows", "after_sequence", "grant_token")
    TERMINAL_ID_FIELD_NUMBER: _ClassVar[int]
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    AFTER_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    GRANT_TOKEN_FIELD_NUMBER: _ClassVar[int]
    terminal_id: str
    cols: int
    rows: int
    after_sequence: int
    grant_token: str
    def __init__(self, terminal_id: _Optional[str] = ..., cols: _Optional[int] = ..., rows: _Optional[int] = ..., after_sequence: _Optional[int] = ..., grant_token: _Optional[str] = ...) -> None: ...

class TerminalDetach(_message.Message):
    __slots__ = ("terminal_id",)
    TERMINAL_ID_FIELD_NUMBER: _ClassVar[int]
    terminal_id: str
    def __init__(self, terminal_id: _Optional[str] = ...) -> None: ...

class TerminalInput(_message.Message):
    __slots__ = ("data", "source")
    DATA_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    data: str
    source: str
    def __init__(self, data: _Optional[str] = ..., source: _Optional[str] = ...) -> None: ...

class TerminalResize(_message.Message):
    __slots__ = ("cols", "rows")
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    cols: int
    rows: int
    def __init__(self, cols: _Optional[int] = ..., rows: _Optional[int] = ...) -> None: ...

class TerminalAcknowledge(_message.Message):
    __slots__ = ("sequence",)
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    def __init__(self, sequence: _Optional[int] = ...) -> None: ...

class TerminalClientMessage(_message.Message):
    __slots__ = ("attach", "detach", "input", "agent_input", "resize", "acknowledge", "json")
    ATTACH_FIELD_NUMBER: _ClassVar[int]
    DETACH_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    AGENT_INPUT_FIELD_NUMBER: _ClassVar[int]
    RESIZE_FIELD_NUMBER: _ClassVar[int]
    ACKNOWLEDGE_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    attach: TerminalAttach
    detach: TerminalDetach
    input: TerminalInput
    agent_input: TerminalInput
    resize: TerminalResize
    acknowledge: TerminalAcknowledge
    json: _terminal_pb2.TerminalJsonMessage
    def __init__(self, attach: _Optional[_Union[TerminalAttach, _Mapping]] = ..., detach: _Optional[_Union[TerminalDetach, _Mapping]] = ..., input: _Optional[_Union[TerminalInput, _Mapping]] = ..., agent_input: _Optional[_Union[TerminalInput, _Mapping]] = ..., resize: _Optional[_Union[TerminalResize, _Mapping]] = ..., acknowledge: _Optional[_Union[TerminalAcknowledge, _Mapping]] = ..., json: _Optional[_Union[_terminal_pb2.TerminalJsonMessage, _Mapping]] = ...) -> None: ...
