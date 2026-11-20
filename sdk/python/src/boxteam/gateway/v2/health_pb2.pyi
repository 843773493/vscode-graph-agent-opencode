from boxteam.common.v1 import service_lifecycle_pb2 as _service_lifecycle_pb2
from boxteam.gateway.v2 import public_pb2 as _public_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GatewayHealth(_message.Message):
    __slots__ = ("gateway_id", "status", "services", "process_id", "development_restart_available", "active_workspace")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    DEVELOPMENT_RESTART_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    status: _service_lifecycle_pb2.ServiceStatus
    services: _containers.RepeatedCompositeFieldContainer[_service_lifecycle_pb2.ServiceHealth]
    process_id: int
    development_restart_available: bool
    active_workspace: _public_pb2.GatewaySearchWorkspaceOwnerDTO
    def __init__(self, gateway_id: _Optional[str] = ..., status: _Optional[_Union[_service_lifecycle_pb2.ServiceStatus, str]] = ..., services: _Optional[_Iterable[_Union[_service_lifecycle_pb2.ServiceHealth, _Mapping]]] = ..., process_id: _Optional[int] = ..., development_restart_available: _Optional[bool] = ..., active_workspace: _Optional[_Union[_public_pb2.GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ...) -> None: ...
