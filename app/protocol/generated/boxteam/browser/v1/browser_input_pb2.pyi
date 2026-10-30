from boxteam.browser.v1 import browser_pb2 as _browser_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BrowserAttach(_message.Message):
    __slots__ = ("browser_id", "participant_id", "grant_token")
    BROWSER_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    GRANT_TOKEN_FIELD_NUMBER: _ClassVar[int]
    browser_id: str
    participant_id: str
    grant_token: str
    def __init__(self, browser_id: _Optional[str] = ..., participant_id: _Optional[str] = ..., grant_token: _Optional[str] = ...) -> None: ...

class BrowserDetach(_message.Message):
    __slots__ = ("browser_id",)
    BROWSER_ID_FIELD_NUMBER: _ClassVar[int]
    browser_id: str
    def __init__(self, browser_id: _Optional[str] = ...) -> None: ...

class BrowserPointer(_message.Message):
    __slots__ = ("action", "x", "y", "button", "delta_x", "delta_y")
    ACTION_FIELD_NUMBER: _ClassVar[int]
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    BUTTON_FIELD_NUMBER: _ClassVar[int]
    DELTA_X_FIELD_NUMBER: _ClassVar[int]
    DELTA_Y_FIELD_NUMBER: _ClassVar[int]
    action: str
    x: float
    y: float
    button: str
    delta_x: float
    delta_y: float
    def __init__(self, action: _Optional[str] = ..., x: _Optional[float] = ..., y: _Optional[float] = ..., button: _Optional[str] = ..., delta_x: _Optional[float] = ..., delta_y: _Optional[float] = ...) -> None: ...

class BrowserKey(_message.Message):
    __slots__ = ("action", "key", "code", "text", "repeat")
    ACTION_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    REPEAT_FIELD_NUMBER: _ClassVar[int]
    action: str
    key: str
    code: str
    text: str
    repeat: bool
    def __init__(self, action: _Optional[str] = ..., key: _Optional[str] = ..., code: _Optional[str] = ..., text: _Optional[str] = ..., repeat: _Optional[bool] = ...) -> None: ...

class BrowserCommand(_message.Message):
    __slots__ = ("name", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    def __init__(self, name: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class BrowserViewportUpdate(_message.Message):
    __slots__ = ("width", "height")
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    width: int
    height: int
    def __init__(self, width: _Optional[int] = ..., height: _Optional[int] = ...) -> None: ...

class BrowserFrameAcknowledge(_message.Message):
    __slots__ = ("frame_id", "received_bytes")
    FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    RECEIVED_BYTES_FIELD_NUMBER: _ClassVar[int]
    frame_id: int
    received_bytes: int
    def __init__(self, frame_id: _Optional[int] = ..., received_bytes: _Optional[int] = ...) -> None: ...

class BrowserClientMessage(_message.Message):
    __slots__ = ("attach", "detach", "pointer", "key", "command", "viewport", "frame_ack", "json")
    ATTACH_FIELD_NUMBER: _ClassVar[int]
    DETACH_FIELD_NUMBER: _ClassVar[int]
    POINTER_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    VIEWPORT_FIELD_NUMBER: _ClassVar[int]
    FRAME_ACK_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    attach: BrowserAttach
    detach: BrowserDetach
    pointer: BrowserPointer
    key: BrowserKey
    command: BrowserCommand
    viewport: BrowserViewportUpdate
    frame_ack: BrowserFrameAcknowledge
    json: _browser_pb2.BrowserJsonMessage
    def __init__(self, attach: _Optional[_Union[BrowserAttach, _Mapping]] = ..., detach: _Optional[_Union[BrowserDetach, _Mapping]] = ..., pointer: _Optional[_Union[BrowserPointer, _Mapping]] = ..., key: _Optional[_Union[BrowserKey, _Mapping]] = ..., command: _Optional[_Union[BrowserCommand, _Mapping]] = ..., viewport: _Optional[_Union[BrowserViewportUpdate, _Mapping]] = ..., frame_ack: _Optional[_Union[BrowserFrameAcknowledge, _Mapping]] = ..., json: _Optional[_Union[_browser_pb2.BrowserJsonMessage, _Mapping]] = ...) -> None: ...
