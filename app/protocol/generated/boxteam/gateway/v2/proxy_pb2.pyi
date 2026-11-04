from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ProxyTarget(_message.Message):
    __slots__ = ("workspace_id", "service", "path", "gateway_id")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    service: str
    path: str
    gateway_id: str
    def __init__(self, workspace_id: _Optional[str] = ..., service: _Optional[str] = ..., path: _Optional[str] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class ProxyResponseMetadata(_message.Message):
    __slots__ = ("status_code", "headers")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    status_code: int
    headers: _containers.ScalarMap[str, str]
    def __init__(self, status_code: _Optional[int] = ..., headers: _Optional[_Mapping[str, str]] = ...) -> None: ...
