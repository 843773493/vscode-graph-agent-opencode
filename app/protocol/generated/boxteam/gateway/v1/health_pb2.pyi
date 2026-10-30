from boxteam.common.v1 import service_lifecycle_pb2 as _service_lifecycle_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GatewayHealth(_message.Message):
    __slots__ = ("gateway_id", "status", "active_workspace_id", "services", "process_id", "development_restart_available")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    DEVELOPMENT_RESTART_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    status: _service_lifecycle_pb2.ServiceStatus
    active_workspace_id: str
    services: _containers.RepeatedCompositeFieldContainer[_service_lifecycle_pb2.ServiceHealth]
    process_id: int
    development_restart_available: bool
    def __init__(self, gateway_id: _Optional[str] = ..., status: _Optional[_Union[_service_lifecycle_pb2.ServiceStatus, str]] = ..., active_workspace_id: _Optional[str] = ..., services: _Optional[_Iterable[_Union[_service_lifecycle_pb2.ServiceHealth, _Mapping]]] = ..., process_id: _Optional[int] = ..., development_restart_available: _Optional[bool] = ...) -> None: ...
