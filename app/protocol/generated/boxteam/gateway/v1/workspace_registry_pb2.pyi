from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WorkspaceStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKSPACE_STATUS_UNSPECIFIED: _ClassVar[WorkspaceStatus]
    WORKSPACE_STATUS_READY: _ClassVar[WorkspaceStatus]
    WORKSPACE_STATUS_OFFLINE: _ClassVar[WorkspaceStatus]
WORKSPACE_STATUS_UNSPECIFIED: WorkspaceStatus
WORKSPACE_STATUS_READY: WorkspaceStatus
WORKSPACE_STATUS_OFFLINE: WorkspaceStatus

class WorkspaceSummary(_message.Message):
    __slots__ = ("workspace_id", "name", "backend_url", "status", "active", "connection_kind")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    BACKEND_URL_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    name: str
    backend_url: str
    status: WorkspaceStatus
    active: bool
    connection_kind: str
    def __init__(self, workspace_id: _Optional[str] = ..., name: _Optional[str] = ..., backend_url: _Optional[str] = ..., status: _Optional[_Union[WorkspaceStatus, str]] = ..., active: _Optional[bool] = ..., connection_kind: _Optional[str] = ...) -> None: ...

class WorkspaceRegistry(_message.Message):
    __slots__ = ("active_workspace_id", "items")
    ACTIVE_WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    active_workspace_id: str
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceSummary]
    def __init__(self, active_workspace_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[WorkspaceSummary, _Mapping]]] = ...) -> None: ...
