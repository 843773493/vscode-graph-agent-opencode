from boxteam.browser.v1 import browser_pb2 as _browser_pb2
from boxteam.common.v1 import error_pb2 as _error_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BrowserPageState(_message.Message):
    __slots__ = ("page",)
    PAGE_FIELD_NUMBER: _ClassVar[int]
    page: _browser_pb2.BrowserPage
    def __init__(self, page: _Optional[_Union[_browser_pb2.BrowserPage, _Mapping]] = ...) -> None: ...

class BrowserFrame(_message.Message):
    __slots__ = ("frame_id", "sequence", "data", "content_type")
    FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    frame_id: str
    sequence: int
    data: bytes
    content_type: str
    def __init__(self, frame_id: _Optional[str] = ..., sequence: _Optional[int] = ..., data: _Optional[bytes] = ..., content_type: _Optional[str] = ...) -> None: ...

class BrowserDownloadReady(_message.Message):
    __slots__ = ("download_id", "filename", "content_type")
    DOWNLOAD_ID_FIELD_NUMBER: _ClassVar[int]
    FILENAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    download_id: str
    filename: str
    content_type: str
    def __init__(self, download_id: _Optional[str] = ..., filename: _Optional[str] = ..., content_type: _Optional[str] = ...) -> None: ...

class BrowserServerMessage(_message.Message):
    __slots__ = ("page_state", "frame", "download_ready", "error", "json")
    PAGE_STATE_FIELD_NUMBER: _ClassVar[int]
    FRAME_FIELD_NUMBER: _ClassVar[int]
    DOWNLOAD_READY_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    page_state: BrowserPageState
    frame: BrowserFrame
    download_ready: BrowserDownloadReady
    error: _error_pb2.Error
    json: _browser_pb2.BrowserJsonMessage
    def __init__(self, page_state: _Optional[_Union[BrowserPageState, _Mapping]] = ..., frame: _Optional[_Union[BrowserFrame, _Mapping]] = ..., download_ready: _Optional[_Union[BrowserDownloadReady, _Mapping]] = ..., error: _Optional[_Union[_error_pb2.Error, _Mapping]] = ..., json: _Optional[_Union[_browser_pb2.BrowserJsonMessage, _Mapping]] = ...) -> None: ...
