import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BrowserStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    BROWSER_STATUS_UNSPECIFIED: _ClassVar[BrowserStatus]
    BROWSER_STATUS_CREATED: _ClassVar[BrowserStatus]
    BROWSER_STATUS_RUNNING: _ClassVar[BrowserStatus]
    BROWSER_STATUS_FROZEN: _ClassVar[BrowserStatus]
    BROWSER_STATUS_DISCARDED: _ClassVar[BrowserStatus]
    BROWSER_STATUS_LOST: _ClassVar[BrowserStatus]
    BROWSER_STATUS_CLOSED: _ClassVar[BrowserStatus]
    BROWSER_STATUS_DELETED: _ClassVar[BrowserStatus]
    BROWSER_STATUS_FAILED: _ClassVar[BrowserStatus]
BROWSER_STATUS_UNSPECIFIED: BrowserStatus
BROWSER_STATUS_CREATED: BrowserStatus
BROWSER_STATUS_RUNNING: BrowserStatus
BROWSER_STATUS_FROZEN: BrowserStatus
BROWSER_STATUS_DISCARDED: BrowserStatus
BROWSER_STATUS_LOST: BrowserStatus
BROWSER_STATUS_CLOSED: BrowserStatus
BROWSER_STATUS_DELETED: BrowserStatus
BROWSER_STATUS_FAILED: BrowserStatus

class BrowserViewport(_message.Message):
    __slots__ = ("width", "height")
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    width: int
    height: int
    def __init__(self, width: _Optional[int] = ..., height: _Optional[int] = ...) -> None: ...

class BrowserPage(_message.Message):
    __slots__ = ("browser_id", "page_id", "title", "url", "viewport", "status", "sequence", "metadata", "created_at", "updated_at", "workspace_id", "transport_owner_revision", "reconciliation_status")
    BROWSER_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    VIEWPORT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    TRANSPORT_OWNER_REVISION_FIELD_NUMBER: _ClassVar[int]
    RECONCILIATION_STATUS_FIELD_NUMBER: _ClassVar[int]
    browser_id: str
    page_id: str
    title: str
    url: str
    viewport: BrowserViewport
    status: BrowserStatus
    sequence: int
    metadata: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    workspace_id: str
    transport_owner_revision: int
    reconciliation_status: str
    def __init__(self, browser_id: _Optional[str] = ..., page_id: _Optional[str] = ..., title: _Optional[str] = ..., url: _Optional[str] = ..., viewport: _Optional[_Union[BrowserViewport, _Mapping]] = ..., status: _Optional[_Union[BrowserStatus, str]] = ..., sequence: _Optional[int] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., workspace_id: _Optional[str] = ..., transport_owner_revision: _Optional[int] = ..., reconciliation_status: _Optional[str] = ...) -> None: ...

class BrowserJsonMessage(_message.Message):
    __slots__ = ("type", "payload")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    type: str
    payload: _struct_pb2.Struct
    def __init__(self, type: _Optional[str] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...
