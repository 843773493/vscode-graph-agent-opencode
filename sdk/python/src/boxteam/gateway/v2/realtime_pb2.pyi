from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class RealtimeRequest(_message.Message):
    __slots__ = ("type", "token", "subscription_id", "path", "method", "headers", "body", "consumed_bytes")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TYPE_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    SUBSCRIPTION_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    CONSUMED_BYTES_FIELD_NUMBER: _ClassVar[int]
    type: str
    token: str
    subscription_id: int
    path: str
    method: str
    headers: _containers.ScalarMap[str, str]
    body: str
    consumed_bytes: int
    def __init__(self, type: _Optional[str] = ..., token: _Optional[str] = ..., subscription_id: _Optional[int] = ..., path: _Optional[str] = ..., method: _Optional[str] = ..., headers: _Optional[_Mapping[str, str]] = ..., body: _Optional[str] = ..., consumed_bytes: _Optional[int] = ...) -> None: ...

class RealtimeResponse(_message.Message):
    __slots__ = ("type", "subscription_id", "status", "headers", "message", "request_id")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SUBSCRIPTION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    type: str
    subscription_id: int
    status: int
    headers: _containers.ScalarMap[str, str]
    message: str
    request_id: str
    def __init__(self, type: _Optional[str] = ..., subscription_id: _Optional[int] = ..., status: _Optional[int] = ..., headers: _Optional[_Mapping[str, str]] = ..., message: _Optional[str] = ..., request_id: _Optional[str] = ...) -> None: ...
