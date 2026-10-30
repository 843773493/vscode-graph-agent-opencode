import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ServiceStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SERVICE_STATUS_UNSPECIFIED: _ClassVar[ServiceStatus]
    SERVICE_STATUS_READY: _ClassVar[ServiceStatus]
    SERVICE_STATUS_DEGRADED: _ClassVar[ServiceStatus]
    SERVICE_STATUS_OFFLINE: _ClassVar[ServiceStatus]
    SERVICE_STATUS_UNAVAILABLE: _ClassVar[ServiceStatus]
SERVICE_STATUS_UNSPECIFIED: ServiceStatus
SERVICE_STATUS_READY: ServiceStatus
SERVICE_STATUS_DEGRADED: ServiceStatus
SERVICE_STATUS_OFFLINE: ServiceStatus
SERVICE_STATUS_UNAVAILABLE: ServiceStatus

class ServiceHealth(_message.Message):
    __slots__ = ("service_id", "status", "health_path", "error", "checked_at")
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HEALTH_PATH_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    CHECKED_AT_FIELD_NUMBER: _ClassVar[int]
    service_id: str
    status: ServiceStatus
    health_path: str
    error: str
    checked_at: _timestamp_pb2.Timestamp
    def __init__(self, service_id: _Optional[str] = ..., status: _Optional[_Union[ServiceStatus, str]] = ..., health_path: _Optional[str] = ..., error: _Optional[str] = ..., checked_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
